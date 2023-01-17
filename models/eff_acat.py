import math

import gpytorch
import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
import random
from torch.autograd import Variable
from models.ACAT import ACAT
from models.BasicAttn import BasicAttn
from models.ConvAttn import ConvAttn
from models.Autoformer import AutoCorrelation
from models.Informer import ProbAttention
from models.KittyCat import KittyCatConv
from models.LogTrans import LogTrans


torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True


def get_attn_subsequent_mask(seq):
    attn_shape = [seq.size(0), seq.size(1), seq.size(1)]
    subsequent_mask = np.triu(np.ones(attn_shape), k=1)
    subsequent_mask = torch.from_numpy(subsequent_mask).int()
    return subsequent_mask


class PositionalEncoding(nn.Module):
    """Positional encoding."""
    def __init__(self, d_hid, device, max_len=1000):
        super(PositionalEncoding, self).__init__()
        # Create a long enough `P`
        self.P = torch.zeros((1, max_len, d_hid)).to(device)
        X = torch.arange(max_len, dtype=torch.float32).reshape(
            -1, 1) / torch.pow(10000, torch.arange(
            0, d_hid, 2, dtype=torch.float32) / d_hid)
        self.P[:, :, 0::2] = torch.sin(X)
        self.P[:, :, 1::2] = torch.cos(X)

    def forward(self, X):
        X = X + self.P[:, :X.shape[1], :].to(X.device)
        return X


class MultiHeadAttention(nn.Module):

    def __init__(self, d_model, d_k, d_v, n_heads, device, attn_type, seed):

        super(MultiHeadAttention, self).__init__()

        self.WQ = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.WK = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.WV = nn.Linear(d_model, d_v * n_heads, bias=False)
        self.fc = nn.Linear(n_heads * d_v, d_model, bias=False)

        self.device = device

        self.d_model = d_model
        self.d_k = d_k
        self.d_v = d_v
        self.n_heads = n_heads
        self.attn_type = attn_type
        self.seed = seed

    def forward(self, Q, K, V, attn_mask):

        batch_size = Q.shape[0]
        q_s = self.WQ(Q).view(batch_size, -1, self.n_heads, self.d_k).transpose(1, 2)
        k_s = self.WK(K).view(batch_size, -1, self.n_heads, self.d_k).transpose(1, 2)
        v_s = self.WV(V).view(batch_size, -1, self.n_heads, self.d_v).transpose(1, 2)

        if attn_mask is not None:
            attn_mask = attn_mask.unsqueeze(1).repeat(1, self.n_heads, 1, 1)
        if self.attn_type == "ACAT":
            context,  attn = ACAT(d_k=self.d_k, device=self.device, h=self.n_heads, l_k=k_s.shape[2], seed=self.seed)(
                Q=q_s, K=k_s, V=v_s, attn_mask=attn_mask)
        elif "KittyCat" in self.attn_type:
            context, attn = KittyCatConv(d_k=self.d_k, device=self.device, h=self.n_heads, l_k=k_s.shape[2], seed=self.seed)(
            Q=q_s, K=k_s, V=v_s, attn_mask=attn_mask)
        elif self.attn_type == "basic_attn":
            context, attn = BasicAttn(d_k=self.d_k, device=self.device, seed=self.seed)(
            Q=q_s, K=k_s, V=v_s, attn_mask=attn_mask)
        elif self.attn_type == "LogTrans":
            context,  attn = LogTrans(d_k=self.d_k, device=self.device, seed=self.seed)(
                Q=q_s, K=k_s, V=v_s, attn_mask=attn_mask)
        elif self.attn_type == "informer":
            mask_flag = True if attn_mask is not None else False
            context, attn = ProbAttention(mask_flag=mask_flag, seed=self.seed)(q_s, k_s, v_s, attn_mask)
        else:
            context, attn = AutoCorrelation()(q_s.transpose(1, 2), k_s.transpose(1, 2), v_s.transpose(1, 2), attn_mask)
        context = context.transpose(1, 2).contiguous().view(batch_size, -1, self.n_heads * self.d_v)
        output = self.fc(context)
        return output, attn


class PoswiseFeedForwardNet(nn.Module):

    def __init__(self, d_model, d_ff):
        super(PoswiseFeedForwardNet, self).__init__()

        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)

        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.uniform_(m.weight, -1/np.sqrt(d_model), 1/np.sqrt(d_model))

    def forward(self, inputs):

        return self.w_2(F.relu(self.w_1(inputs)))


class EncoderLayer(nn.Module):

    def __init__(self, d_model, d_ff, d_k, d_v, n_heads,
                 device, attn_type, seed):
        super(EncoderLayer, self).__init__()
        self.enc_self_attn = MultiHeadAttention(
            d_model=d_model, d_k=d_k,
            d_v=d_v, n_heads=n_heads, device=device,
            attn_type=attn_type, seed=seed)
        self.pos_ffn = PoswiseFeedForwardNet(
            d_model=d_model, d_ff=d_ff)
        self.layer_norm = nn.LayerNorm(d_model, elementwise_affine=False)

    def forward(self, enc_inputs, enc_self_attn_mask=None):

        out, attn = self.enc_self_attn(enc_inputs, enc_inputs, enc_inputs, attn_mask=enc_self_attn_mask)
        out = self.layer_norm(out + enc_inputs)
        out_2 = self.pos_ffn(out)
        out_2 = self.layer_norm(out_2 + out)
        return out_2, attn


class Encoder(nn.Module):

    def __init__(self, d_model, d_ff, d_k, d_v, n_heads,
                 n_layers, pad_index, device,
                 attn_type, seed):
        super(Encoder, self).__init__()
        self.device = device
        self.pad_index = pad_index
        self.attn_type = attn_type
        self.pos_emb = PositionalEncoding(
            d_hid=d_model,
            device=device)
        self.n_layers = n_layers
        self.layers = []
        for _ in range(n_layers):
            encoder_layer = EncoderLayer(
                d_model=d_model, d_ff=d_ff,
                d_k=d_k, d_v=d_v, n_heads=n_heads,
                device=device,
                attn_type=attn_type, seed=seed)
            self.layers.append(encoder_layer)
        self.layers = nn.ModuleList(self.layers)

    def forward(self, enc_input):

        enc_outputs = self.pos_emb(enc_input)

        enc_self_attn_mask = None

        enc_self_attns = []
        for layer in self.layers:
            enc_outputs, enc_self_attn = layer(enc_outputs, enc_self_attn_mask)
            enc_self_attns.append(enc_self_attn)

        '''enc_self_attns = torch.stack(enc_self_attns)
        enc_self_attns = enc_self_attns.permute([1, 0, 2, 3, 4])'''
        return enc_outputs, enc_self_attns


class DecoderLayer(nn.Module):

    def __init__(self, d_model, d_ff, d_k, d_v,
                 n_heads, device, attn_type, seed):
        super(DecoderLayer, self).__init__()
        self.dec_self_attn = MultiHeadAttention(
            d_model=d_model, d_k=d_k,
            d_v=d_v, n_heads=n_heads, device=device,
            attn_type=attn_type, seed=seed)
        self.dec_enc_attn = MultiHeadAttention(
            d_model=d_model, d_k=d_k,
            d_v=d_v, n_heads=n_heads, device=device,
            attn_type=attn_type, seed=seed)
        self.pos_ffn = PoswiseFeedForwardNet(
            d_model=d_model, d_ff=d_ff)
        self.layer_norm = nn.LayerNorm(d_model, elementwise_affine=False)

    def forward(self, dec_inputs, enc_outputs, dec_self_attn_mask=None, dec_enc_attn_mask=None):

        out, dec_self_attn = self.dec_self_attn(dec_inputs, dec_inputs, dec_inputs, dec_self_attn_mask)
        out = self.layer_norm(dec_inputs + out)
        out2, dec_enc_attn = self.dec_enc_attn(out, enc_outputs, enc_outputs, dec_enc_attn_mask)
        out2 = self.layer_norm(out + out2)
        out3 = self.pos_ffn(out2)
        out3 = self.layer_norm(out2 + out3)
        return out3, dec_self_attn, dec_enc_attn


class Decoder(nn.Module):

    def __init__(self, d_model, d_ff, d_k, d_v,
                 n_heads, n_layers, pad_index, device,
                 attn_type, seed):
        super(Decoder, self).__init__()
        self.pad_index = pad_index
        self.device = device
        self.attn_type = attn_type
        self.pos_emb = PositionalEncoding(
            d_hid=d_model,
            device=device)
        self.layer_norm = nn.LayerNorm(d_model)
        self.layers = []
        for _ in range(n_layers):
            decoder_layer = DecoderLayer(
                d_model=d_model, d_ff=d_ff,
                d_k=d_k, d_v=d_v,
                n_heads=n_heads, device=device,
                attn_type=attn_type, seed=seed)
            self.layers.append(decoder_layer)
        self.layers = nn.ModuleList(self.layers)
        self.d_k = d_k

    def forward(self, dec_inputs, enc_outputs):

        dec_outputs = self.pos_emb(dec_inputs)

        dec_self_attn_subsequent_mask = get_attn_subsequent_mask(dec_inputs)

        dec_self_attns, dec_enc_attns = [], []
        for layer in self.layers:
            dec_outputs, dec_self_attn, dec_enc_attn = layer(
                dec_inputs=dec_outputs,
                enc_outputs=enc_outputs,
                dec_self_attn_mask=dec_self_attn_subsequent_mask,
                dec_enc_attn_mask=None,
            )
            dec_self_attns.append(dec_self_attn)
            dec_enc_attns.append(dec_enc_attn)
        '''dec_self_attns = torch.stack(dec_self_attns)
        dec_enc_attns = torch.stack(dec_enc_attns)

        dec_self_attns = dec_self_attns.permute([1, 0, 2, 3, 4])
        dec_enc_attns = dec_enc_attns.permute([1, 0, 2, 3, 4])'''

        return dec_outputs, dec_self_attns, dec_enc_attns


def normal_kl(mean1, logvar1, mean2, logvar2):
    total_kl = 0.5 * (
        -1.0
        + logvar2
        - logvar1
        + torch.exp(logvar1 - logvar2)
        + ((mean1 - mean2) ** 2) * torch.exp(-logvar2)
    )

    return total_kl


class process_model(nn.Module):
    def __init__(self, gp,  d, device):
        super(process_model, self).__init__()

        self.encoder = nn.Sequential(nn.Conv1d(in_channels=d, out_channels=d, kernel_size=3, padding=int((3-1)/2)),
                                     nn.Conv1d(in_channels=d, out_channels=d, kernel_size=3, padding=int((3-1)/2)),
                                     nn.BatchNorm1d(d),
                                     nn.Softmax(dim=-1),).to(device)

        self.musig = nn.Linear(d, 2*d, device=device)

        self.decoder = nn.Sequential(
            nn.Conv1d(in_channels=d, out_channels=d, kernel_size=3, padding=int((3 - 1) / 2)),
            nn.Conv1d(in_channels=d, out_channels=d, kernel_size=3, padding=int((3 - 1) / 2)),
            nn.BatchNorm1d(d),
            nn.Softmax(dim=-1), ).to(device)

        self.norm = nn.LayerNorm(d)

        self.mean_module = gpytorch.means.ConstantMean()
        self.covar_module = gpytorch.kernels.ScaleKernel(gpytorch.kernels.RBFKernel())
        self.mean_module_t = gpytorch.means.ConstantMean()
        self.covar_module_t = gpytorch.kernels.ScaleKernel(gpytorch.kernels.RBFKernel())
        self.gp = gp
        if self.gp:
            self.gp_proj_mean = nn.Linear(1, d)
            self.gp_proj_var = nn.Linear(1, d)

        self.d = d
        self.device = device

    def forward(self, x, target=None):

        eps = torch.randn_like(x)

        if target is not None:
            s_len = target.shape[1]
            mean_t = self.mean_module_t(target)
            co_var_t = self.covar_module_t(target)

            dist = gpytorch.distributions.MultivariateNormal(mean_t, co_var_t)
            mean_t = dist.mean.unsqueeze(-1)
            co_var_t = dist.variance.unsqueeze(-1)

        if self.gp:
            b, s, _ = x.shape
            mean = self.mean_module(x)
            co_var = self.covar_module(x)

            dist = gpytorch.distributions.MultivariateNormal(mean, co_var)
            mean = dist.mean.unsqueeze(-1)
            co_var = dist.variance.unsqueeze(-1)

            eps = self.gp_proj_mean(mean) + self.gp_proj_var(co_var) * eps * 0.1
            x_noisy = x.add_(eps)

        else:
            x_noisy = x.add_(eps * 0.1)

        musig = self.musig(self.encoder(x_noisy.permute(0, 2, 1)).permute(0, 2, 1))

        mu, sigma = musig[:, :, :self.d], musig[:, :, -self.d:]

        z = mu + torch.exp(sigma*0.5) * torch.randn_like(sigma, device=self.device)

        y = self.decoder(z.permute(0, 2, 1)).permute(0, 2, 1)

        output = self.norm(y + x)

        if target is not None:

            mean_t = torch.flatten(torch.mean(mean_t, dim=-1), start_dim=1)
            co_var_t = torch.flatten(torch.mean(co_var_t, dim=-1), start_dim=1)

            mu = torch.flatten(torch.mean(mu[:, -s_len:, :], dim=-1), start_dim=1)
            sigma = torch.flatten(torch.mean(sigma[:, -s_len:, :], dim=-1), start_dim=1)

            kl_loss = normal_kl(mean_t, co_var_t, mu, sigma).mean()

        else:
            kl_loss = 0

        return output, kl_loss


class Transformer(nn.Module):

    def __init__(self, src_input_size, tgt_input_size, pred_len, d_model,
                 d_ff, d_k, d_v, n_heads, n_layers, src_pad_index,
                 tgt_pad_index, device, attn_type, seed, p_model, gp):
        super(Transformer, self).__init__()

        torch.manual_seed(seed)
        random.seed(seed)
        np.random.seed(seed)

        self.attn_type = attn_type

        self.encoder = Encoder(
            d_model=d_model, d_ff=d_ff,
            d_k=d_k, d_v=d_v, n_heads=n_heads,
            n_layers=n_layers, pad_index=src_pad_index,
            device=device, attn_type=attn_type, seed=seed)
        self.decoder = Decoder(
            d_model=d_model, d_ff=d_ff,
            d_k=d_k, d_v=d_v, n_heads=n_heads,
            n_layers=1, pad_index=tgt_pad_index,
            device=device,
            attn_type=attn_type, seed=seed)

        self.enc_embedding = nn.Linear(src_input_size, d_model)
        self.dec_embedding = nn.Linear(tgt_input_size, d_model)
        self.projection = nn.Linear(d_model, 1, bias=False)
        self.p_model = p_model
        if self.p_model:
            self.process = process_model(gp, d_model, device)
        self.attn_type = attn_type
        self.pred_len = pred_len
        self.device = device

    def forward(self, enc_inputs, dec_inputs, target=None):

        enc_outputs = self.enc_embedding(enc_inputs)
        dec_outputs = self.dec_embedding(dec_inputs)

        enc_outputs, enc_self_attns = self.encoder(enc_outputs)
        dec_outputs, dec_self_attns, dec_enc_attn = self.decoder(dec_outputs, enc_outputs)

        if self.p_model:

            outputs, kl_loss = self.process(dec_outputs, target)
            outputs = self.projection(outputs[:, -self.pred_len:, :])
            return outputs, kl_loss

        else:

            outputs = self.projection(dec_outputs[:, -self.pred_len:, :])
            return outputs
