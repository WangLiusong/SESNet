import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops.layers.torch import Rearrange

def chunkwise(xs, N_l, N_c, N_r):
    """Slice input frames chunk by chunk.

    Args:
        xs (FloatTensor): `[B, T, input_dim]`
        N_l (int): number of frames for left context
        N_c (int): number of frames for current context
        N_r (int): number of frames for right context
    Returns:
        xs (FloatTensor): `[B * n_chunks, N_l + N_c + N_r, input_dim]`
            where n_chunks = ceil(T / N_c)
    """
    bs, xmax, idim = xs.size()
    n_chunks = math.ceil(xmax / N_c)
    c = N_l + N_c + N_r
    s_index = torch.arange(0, xmax, N_c).unsqueeze(-1)
    c_index = torch.arange(0, c)
    index = s_index + c_index #(xmax,c)
    xs_pad = torch.cat([xs.new_zeros(bs, N_l, idim),
                        xs,
                        xs.new_zeros(bs, N_c * n_chunks - xmax + N_r, idim)], dim=1)#B,C+T-1,D
    xs_chunk = xs_pad[:, index].contiguous().view(bs * n_chunks, N_l + N_c + N_r, idim)#B*T,C,D
    return xs_chunk

class MHLocalDenseSynthesizerAttention(nn.Module):
    """Multi-Head Local Dense Synthesizer attention layer
    In this implementation, the calculation of multi-head mechanism is similar to that of self-attention,
    but it takes more time for training. We provide an alternative multi-head mechanism implementation
    that can achieve competitive results with less time.

    :param int n_head: the number of heads
    :param int n_feat: the dimension of features
    :param float dropout_rate: dropout rate
    :param int context_size: context size
    :param bool use_bias: use bias term in linear layers
    """

    def __init__(self, n_head, n_feat, dropout_rate, context_size=3, use_bias=False):
        super().__init__()
        assert n_feat % n_head == 0
        # We assume d_v always equals d_k
        self.d_k = n_feat // n_head
        self.h = n_head
        self.c = context_size
        self.w1 = nn.Linear(n_feat, n_feat, bias=use_bias)
        # self.w2 = nn.Linear(n_feat, n_head * self.c, bias=use_bias)
        self.w2 = nn.Conv1d(in_channels=n_feat, out_channels=n_head * self.c, kernel_size=1,
                            groups=n_head)
        self.w3 = nn.Linear(n_feat, n_feat, bias=use_bias)
        self.w_out = nn.Linear(n_feat, n_feat, bias=use_bias)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout_rate)

    def forward(self, query, key, value, mask):
        """Forward pass.

                :param torch.Tensor query: (batch, time, size)
                :param torch.Tensor key: (batch, time, size) dummy
                :param torch.Tensor value: (batch, time, size)
                :param torch.Tensor mask: (batch, time, time) dummy
                :return torch.Tensor: attentioned and transformed `value` (batch, time, d_model)
                """
        bs, time = query.size()[: 2]
        query = self.w1(query)  # [B, T, d]
        # [B, T, d] --> [B, d, T] --> [B, H*c, T]
        weight = self.w2(torch.relu(query).transpose(1, 2))
        # [B, H, c, T] --> [B, T, H, c] --> [B*T, H, 1, c]
        weight = weight.view(bs, self.h, self.c, time).permute(0, 3, 1, 2) \
            .contiguous().view(bs * time, self.h, 1, self.c)
        value = self.w3(value)  # [B, T, d]
        # [B*T, c, d] --> [B*T, c, H, d_k] --> [B*T, H, c, d_k]
        value_cw = chunkwise(value, (self.c - 1) // 2, 1, (self.c - 1) // 2) \
            .view(bs * time, self.c, self.h, self.d_k).transpose(1, 2)
        self.attn = torch.softmax(weight, dim=-1)
        p_attn = self.dropout(self.attn)
        x = torch.matmul(p_attn, value_cw)
        x = x.contiguous().view(bs, -1, self.h * self.d_k)  # [B, T, d]
        x = self.w_out(x)  # [B, T, d]
        return x


####Conformer

class ConformerEncoderLayer(nn.Module):

    def __init__(self, d_model, nhead, bidirectional=True, dropout=0):
        super(ConformerEncoderLayer, self).__init__()
        self.self_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)
        # Implementation of Feedforward model

        self.gru1 = nn.GRU(d_model, d_model * 2, 1, bidirectional=bidirectional)
        self.dropout4 = nn.Dropout(dropout)
        self.dropout5 = nn.Dropout(dropout)
        self.norm4 = nn.LayerNorm(d_model)
        if bidirectional:
            self.linear4 = nn.Linear(d_model * 2 * 2, d_model)
        else:
            self.linear4 = nn.Linear(d_model * 2, d_model)


        self.gru = nn.GRU(d_model, d_model*2, 1, bidirectional=bidirectional)
        self.dropout = nn.Dropout(dropout)
        if bidirectional:
            self.linear2 = nn.Linear(d_model*2*2, d_model)
        else:
            self.linear2 = nn.Linear(d_model*2, d_model)

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)

        self.mhldsa = MHLocalDenseSynthesizerAttention(nhead, d_model, dropout_rate=dropout, context_size=3)
        self.norm3 = nn.LayerNorm(d_model)
        self.dropout3 = nn.Dropout(dropout)


    def __setstate__(self, state):
        if 'activation' not in state:
            state['activation'] = F.relu
        super(ConformerEncoderLayer, self).__setstate__(state)

    def forward(self, src, src_mask=None, src_key_padding_mask=None):
        r"""Pass the input through the encoder layer.
        Args:
            src: the sequnce to the encoder layer (required).
            src_mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).
        Shape:
            see the docs in Transformer class.
        """
        # fist feed forward network
        self.gru1.flatten_parameters()
        out, h_n = self.gru1(src)
        del h_n
        src2 = self.linear4(self.dropout4(F.relu(out)))
        src = src + self.dropout5(src2)
        src = self.norm4(src)

        #Hybrid Attention layer
        src2 = self.self_attn(src, src, src, attn_mask=src_mask,
                              key_padding_mask=src_key_padding_mask)[0]
        src = src + self.dropout1(src2)
        src = self.norm1(src)


        #Multi-Head Local Dense Synthesizer attention layer
        src3 = self.mhldsa(src, src, src, mask=src_mask)
        src = src + self.dropout3(src3)
        src = self.norm3(src)

        # secondly feed forward network
        #gru
        self.gru.flatten_parameters()
        out, h_n = self.gru(src)
        del h_n
        src2 = self.linear2(self.dropout(F.relu(out)))
        src = src + self.dropout2(src2)
        src = self.norm2(src)
        return src

##########Convformer Conv Moudle##############
def exists(val):
    return val is not None


def default(val, d):
    return val if exists(val) else d


def calc_same_padding(kernel_size):
    pad = kernel_size // 2
    return (pad, pad - (kernel_size + 1) % 2)


class Swish(nn.Module):
    def forward(self, x):
        return x * x.sigmoid()


class GLU(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self, x):
        out, gate = x.chunk(2, dim=self.dim)
        return out * gate.sigmoid()


class DepthWiseConv1d(nn.Module):
    def __init__(self, chan_in, chan_out, kernel_size, padding):
        super().__init__()
        self.padding = padding
        self.conv = nn.Conv1d(chan_in, chan_out, kernel_size, groups = chan_in)

    def forward(self, x):
        x = F.pad(x, self.padding)
        return self.conv(x)

class ConformerConvModule(nn.Module):
    def __init__(
        self,
        dim,
        causal = False,
        expansion_factor = 2,
        kernel_size = 31,
        dropout = 0.):
        super().__init__()

        inner_dim = dim * expansion_factor
        padding = calc_same_padding(kernel_size) if not causal else (kernel_size - 1, 0)

        self.net = nn.Sequential(
            # nn.LayerNorm(dim),
            Rearrange('b n c -> b c n'),
            nn.Conv1d(dim, inner_dim * 2, 1),
            GLU(dim=1),
            DepthWiseConv1d(inner_dim, inner_dim, kernel_size = kernel_size, padding = padding),
            nn.BatchNorm1d(inner_dim) if not causal else nn.Identity(),
            Swish(),
            nn.Conv1d(inner_dim, dim, 1),
            Rearrange('b c n -> b n c'),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        return self.net(x)


class Dual_Transformer(nn.Module):
    """
    Deep duaL-path RNN.
    args:
        rnn_type: string, select from 'RNN', 'LSTM' and 'GRU'.
        input_size: int, dimension of the input feature. The input should have shape
                    (batch, seq_len, input_size).
        hidden_size: int, dimension of the hidden state.
        output_size: int, dimension of the output size.
        dropout: float, dropout ratio. Default is 0.
        num_layers: int, number of stacked RNN layers. Default is 1.
        bidirectional: bool, whether the RNN layers are bidirectional. Default is False.
    """

    def __init__(self, input_size, output_size, dropout=0, num_layers=1):
        super(Dual_Transformer, self).__init__()

        self.input_size = input_size
        self.output_size = output_size

        self.input = nn.Sequential(
            nn.Conv2d(input_size, input_size // 2, kernel_size=(1, 1)),       #通道数减半
            nn.PReLU()
        )

        # dual-path RNN
        self.row_trans = nn.ModuleList([])    #local
        self.col_trans = nn.ModuleList([])    #global
        self.row_norm = nn.ModuleList([])
        self.col_norm = nn.ModuleList([])
        for i in range(num_layers):
            self.row_trans.append(ConformerEncoderLayer(d_model=input_size//2, nhead=4, dropout=dropout, bidirectional=True))
            self.col_trans.append(ConformerEncoderLayer(d_model=input_size//2, nhead=4, dropout=dropout, bidirectional=True))
            self.row_norm.append(nn.GroupNorm(1, input_size//2, eps=1e-8))
            self.col_norm.append(nn.GroupNorm(1, input_size//2, eps=1e-8))

        # output layer
        self.output = nn.Sequential(nn.PReLU(),
                                    nn.Conv2d(input_size//2, output_size, (1, 1))       # inchannels=32 , outchannels=32
                                    )

    def forward(self, input):
        #  input --- [b,  c,  num_frames, frame_size]  --- [b, c, dim2, dim1]
        b, c, dim2, dim1 = input.shape
        output = self.input(input)
        for i in range(len(self.row_trans)):
            row_input = output.permute(3, 0, 2, 1).contiguous().view(dim1, b*dim2, -1)  # [dim1, b*dim2, c]
            row_output = self.row_trans[i](row_input)  # [dim1, b*dim2, c]
            row_output = row_output.view(dim1, b, dim2, -1).permute(1, 3, 2, 0).contiguous()  # [b, c, dim2, dim1]
            row_output = self.row_norm[i](row_output)  # [b, c, dim2, dim1]
            output = output + row_output  # [b, c, dim2, dim1]

            col_input = output.permute(2, 0, 3, 1).contiguous().view(dim2, b*dim1, -1)  # [dim2, b*dim1, c]
            col_output = self.col_trans[i](col_input)  # [dim2, b*dim1, c]
            col_output = col_output.view(dim2, b, dim1, -1).permute(1, 3, 0, 2).contiguous()  # [b, c, dim2, dim1]
            col_output = self.col_norm[i](col_output)  # [b, c, dim2, dim1]
            output = output + col_output  # [b, c, dim2, dim1]

        del row_input, row_output, col_input, col_output
        output = self.output(output)  # [b, c, dim2, dim1]

        return output


class SwitchNorm2d(nn.Module):
    def __init__(self, num_features, eps=1e-5, momentum=0.9, using_moving_average=True, using_bn=True,
                 last_gamma=False):
        super(SwitchNorm2d, self).__init__()
        self.eps = eps
        self.momentum = momentum
        self.using_moving_average = using_moving_average
        self.using_bn = using_bn
        self.last_gamma = last_gamma
        self.weight = nn.Parameter(torch.ones(1, num_features, 1, 1))
        self.bias = nn.Parameter(torch.zeros(1, num_features, 1, 1))
        if self.using_bn:
            self.mean_weight = nn.Parameter(torch.ones(3))
            self.var_weight = nn.Parameter(torch.ones(3))
        else:
            self.mean_weight = nn.Parameter(torch.ones(2))
            self.var_weight = nn.Parameter(torch.ones(2))
        if self.using_bn:
            self.register_buffer('running_mean', torch.zeros(1, num_features, 1))
            self.register_buffer('running_var', torch.zeros(1, num_features, 1))

        self.reset_parameters()

    def reset_parameters(self):
        if self.using_bn:
            self.running_mean.zero_()
            self.running_var.zero_()
        if self.last_gamma:
            self.weight.data.fill_(0)
        else:
            self.weight.data.fill_(1)
        self.bias.data.zero_()

    def _check_input_dim(self, input):
        if input.dim() != 4:
            raise ValueError('expected 4D input (got {}D input)'
                             .format(input.dim()))

    def forward(self, x):
        self._check_input_dim(x)
        N, C, H, W = x.size()
        x = x.view(N, C, -1)
        mean_in = x.mean(-1, keepdim=True)
        var_in = x.var(-1, keepdim=True)

        mean_ln = mean_in.mean(1, keepdim=True)
        temp = var_in + mean_in ** 2
        var_ln = temp.mean(1, keepdim=True) - mean_ln ** 2

        if self.using_bn:
            if self.training:
                mean_bn = mean_in.mean(0, keepdim=True)
                var_bn = temp.mean(0, keepdim=True) - mean_bn ** 2
                if self.using_moving_average:
                    self.running_mean.mul_(self.momentum)
                    self.running_mean.add_((1 - self.momentum) * mean_bn.data)
                    self.running_var.mul_(self.momentum)
                    self.running_var.add_((1 - self.momentum) * var_bn.data)
                else:
                    self.running_mean.add_(mean_bn.data)
                    self.running_var.add_(mean_bn.data ** 2 + var_bn.data)
            else:
                mean_bn = torch.autograd.Variable(self.running_mean)
                var_bn = torch.autograd.Variable(self.running_var)

        softmax = nn.Softmax(0)
        mean_weight = softmax(self.mean_weight)
        var_weight = softmax(self.var_weight)

        if self.using_bn:
            mean = mean_weight[0] * mean_in + mean_weight[1] * mean_ln + mean_weight[2] * mean_bn
            var = var_weight[0] * var_in + var_weight[1] * var_ln + var_weight[2] * var_bn
        else:
            mean = mean_weight[0] * mean_in + mean_weight[1] * mean_ln
            var = var_weight[0] * var_in + var_weight[1] * var_ln

        x = (x-mean) / (var+self.eps).sqrt()
        x = x.view(N, C, H, W)
        return x * self.weight + self.bias

class STFT(nn.Module):
    def __init__(self, n_fft=512, hop_length=100, window_length=400):
        super(STFT, self).__init__()
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.window_length = window_length
        self.Stft = torch.stft
    def forward(self, x):
        x = x.squeeze(1)
        x = self.Stft(x, n_fft=self.n_fft, hop_length=self.hop_length, win_length=self.window_length, return_complex=False)[:, :-1, :, :]
        c = x.permute(0, 3, 2, 1)
        return c

class ISTFT(nn.Module):
    def __init__(self, n_fft=512, hop_length=100, window_length=400):
        super(ISTFT, self).__init__()
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.window_length = window_length
        self.Istft = torch.istft
        self.pad = torch.nn.ZeroPad2d((0, 1, 0, 0))
    def forward(self, x):
        out = self.pad(x)
        out = out.permute(0, 3, 2, 1)
        out = torch.istft(out, n_fft=self.n_fft, hop_length=self.hop_length, win_length=self.window_length, return_complex=False)
        out = out.unsqueeze(1)
        return out

class SelectFusion(nn.Module):
    def __init__(self, N):
        super(SelectFusion, self).__init__()
        # Hyper-parameter
        self.N = N
        self.linear3 = nn.Conv2d(2*N, N, kernel_size=(1, 1), bias=False)

    def forward(self, stft_feature, conv_feature):
        fusion_feature = self.linear3(torch.cat([stft_feature, conv_feature], dim=1))
        ratio_mask1 = torch.sigmoid(fusion_feature)
        ratio_mask2 = 1 - ratio_mask1
        conv_out = conv_feature * ratio_mask1
        stft_out = stft_feature * ratio_mask2
        fusion_out = conv_out + stft_out
        out = F.relu(stft_feature + conv_feature + fusion_out)

        return out

class RHConv3(nn.Module):
    def __init__(self, inchannel, outchannel, i):
        super(RHConv3, self).__init__()
        self.conv3x1 = nn.Conv2d(inchannel, outchannel, (3, 1), padding=(1*i, 0), dilation=i)
        self.conv1x3 = nn.Conv2d(inchannel, outchannel, (1, 3), padding=(0, 1*i), dilation=i)
        self.conv3x3 = nn.Conv2d(outchannel, outchannel, (3, 3), padding=1*i, dilation=i)
        self.sn = SwitchNorm2d(outchannel)
        self.act1 = nn.PReLU()
        self.shortcut = nn.Conv2d(inchannel, outchannel, (1, 1), bias=False)
        self.act2 = nn.Sequential(
            SwitchNorm2d(outchannel),
            nn.PReLU(),
        )
    def forward(self, x):
        out3x1 = self.conv3x1(x)
        out1x3 = self.conv1x3(x)
        out = out3x1 + out1x3
        out3x3 = self.conv3x3(out)
        out = self.act1(self.sn(out3x3))
        identity = self.shortcut(x)
        out = self.act2(out+identity)
        return out

class RHConv5(nn.Module):
    def __init__(self, inchannel, outchannel, i):
        super(RHConv5, self).__init__()
        self.conv5x1 = nn.Conv2d(inchannel, outchannel, (5, 1), padding=(2*i, 0), dilation=i)
        self.conv1x5 = nn.Conv2d(inchannel, outchannel, (1, 5), padding=(0, 2*i), dilation=i)
        self.conv5x5 = nn.Conv2d(outchannel, outchannel, (5, 5), padding=2*i, dilation=i)
        self.sn = SwitchNorm2d(outchannel)
        self.act1 = nn.PReLU()
        self.shortcut = nn.Conv2d(inchannel, outchannel, (1, 1), bias=False)
        self.act2 = nn.Sequential(
            SwitchNorm2d(outchannel),
            nn.PReLU(),
        )
    def forward(self, x):
        out5x1 = self.conv5x1(x)
        out1x5 = self.conv1x5(x)
        out = out5x1 + out1x5
        out5x5 = self.conv5x5(out)
        out = self.act1(self.sn(out5x5))
        identity = self.shortcut(x)
        out = self.act2(out+identity)
        return out

class SPConvTranspose2d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, r=1):
        # upconvolution only along second dimension of image
        # Upsampling using sub pixel layers
        super(SPConvTranspose2d, self).__init__()
        self.out_channels = out_channels
        self.conv = nn.Conv2d(in_channels, out_channels * r, kernel_size=kernel_size, stride=(1, 1))
        self.r = r

    def forward(self, x):
        out = self.conv(x)
        batch_size, nchannels, H, W = out.shape
        out = out.view((batch_size, self.r, nchannels // self.r, H, W))
        out = out.permute(0, 2, 3, 4, 1)
        out = out.contiguous().view((batch_size, nchannels // self.r, H, -1))
        return out

class DenseBlock(nn.Module):
    def __init__(self, depth=5, in_channels=64):
        super(DenseBlock, self).__init__()
        self.depth = depth
        self.in_channels = in_channels
        self.pad = nn.ConstantPad2d((1, 1, 1, 0), value=0.)
        self.twidth = 2
        self.kernel_size = (self.twidth, 3)
        for i in range(self.depth):
            dil = 2 ** i
            pad_length = self.twidth + (dil - 1) * (self.twidth - 1) - 1
            setattr(self, 'pad{}'.format(i + 1), nn.ConstantPad2d((1, 1, pad_length, 0), value=0.))
            setattr(self, 'conv{}'.format(i + 1),
                    nn.Conv2d(self.in_channels * (i + 1), self.in_channels, kernel_size=self.kernel_size,
                              dilation=(dil, 1)))
            setattr(self, 'norm{}'.format(i + 1), SwitchNorm2d(self.in_channels))
            setattr(self, 'prelu{}'.format(i + 1), nn.PReLU(self.in_channels))


    def forward(self, x):
        skip = x
        for i in range(self.depth):
            out = getattr(self, 'pad{}'.format(i + 1))(skip)
            out = getattr(self, 'conv{}'.format(i + 1))(out)
            out = getattr(self, 'norm{}'.format(i + 1))(out)
            out = getattr(self, 'prelu{}'.format(i + 1))(out)
            skip = torch.cat([out, skip], dim=1)
        return out

class TF_AuxUnit3(nn.Module):
    def __init__(self,):
        super(TF_AuxUnit3, self).__init__()
        self.rhconv1 = RHConv3(32, 32, 1)
        self.rhconv2 = RHConv3(32, 32, 2)
        self.rhconv3 = RHConv3(32, 32, 4)
        self.rhconv4 = RHConv3(32, 32, 8)
    def forward(self, x):
        x_tf = self.rhconv4(self.rhconv3(self.rhconv2(self.rhconv1(x))))
        return x_tf

class TF_AuxUnit5(nn.Module):
    def __init__(self,):
        super(TF_AuxUnit5, self).__init__()
        self.rhconv1 = RHConv5(32, 32, 1)
        self.rhconv2 = RHConv5(32, 32, 2)
        self.rhconv3 = RHConv5(32, 32, 4)
        self.rhconv4 = RHConv5(32, 32, 8)
    def forward(self, x):
        x_tf = self.rhconv4(self.rhconv3(self.rhconv2(self.rhconv1(x))))
        return x_tf


class Encoder(nn.Module):
    def __init__(self, width=64):
        super(Encoder, self).__init__()
        self.stft = STFT(n_fft=512, hop_length=100, window_length=400)
        self.in_channels = 2
        self.width = width
        self.inp_conv = nn.Conv2d(in_channels=self.in_channels, out_channels=self.width //2,
                                  kernel_size=(1, 1))  # [b, 64, nframes, 512]
        self.inp_snorm = SwitchNorm2d(32)
        self.inp_prelu = nn.PReLU(self.width//2)
        self.branch3 = TF_AuxUnit3()
        self.branch5 = TF_AuxUnit5()
        self.sf = SelectFusion(self.width//2)
    def forward(self, input):
        x = self.stft(input)
        x = self.inp_prelu(self.inp_snorm(self.inp_conv(x)))
        branch3 = self.branch3(x)
        branch5 = self.branch5(x)
        out = self.sf(branch3, branch5)
        return out

class Mask(nn.Module):
    def __init__(self,):
        super(Mask, self).__init__()
        # Hyper-parameter
        self.output1 = nn.Sequential(
            nn.Conv2d(in_channels=64, out_channels=64, kernel_size=(1, 1)),
            nn.Tanh()
        )
        self.output2 = nn.Sequential(
            nn.Conv2d(in_channels=64, out_channels=64, kernel_size=(1, 1)),
            nn.Sigmoid()
        )
        self.maskconv = nn.Conv2d(in_channels=64, out_channels=64, kernel_size=(1, 1))
        self.maskrelu = nn.ReLU(inplace=True)

    def forward(self, input, mask):
        out = self.output1(mask) * self.output2(mask)  # mask [b, 64, T, 100]
        out = self.maskrelu(self.maskconv(out))  # mask
        out = input * out
        return out

class Decoder(nn.Module):
    def __init__(self, width=64):
        super(Decoder, self).__init__()
        # Hyper-parameter
        self.width = width
        self.out_channels = 2
        self.pad1 = nn.ConstantPad2d((1, 1, 0, 0), value=0.)
        self.dec_conv = nn.Conv2d(in_channels=self.width, out_channels=self.width//2, kernel_size=(1, 1))
        self.dec_snorm = SwitchNorm2d(32)
        self.dec_prelu = nn.PReLU(self.width//2)
        self.branch3 = TF_AuxUnit3()
        self.branch5 = TF_AuxUnit5()
        self.sf = SelectFusion(self.width//2)
        self.dec_conv1 = SPConvTranspose2d(in_channels=self.width//2, out_channels=self.width//2, kernel_size=(1, 3), r=2)
        self.dec_snorm1 = SwitchNorm2d(32)
        self.dec_prelu1 = nn.PReLU(self.width//2)
        self.out_conv = nn.Conv2d(in_channels=self.width//2, out_channels=self.out_channels, kernel_size=(1, 1))
        self.istft = ISTFT(n_fft=512, hop_length=100, window_length=400)
    def forward(self, x):
        x = self.dec_prelu(self.dec_snorm(self.dec_conv(x)))
        out1 = self.branch3(x)
        out2 = self.branch5(x)
        out = self.sf(out1, out2)
        out = self.dec_prelu1(self.dec_snorm1(self.dec_conv1(self.pad1(out))))
        out = self.out_conv(out)
        out = self.istft(out)  # [B, C, LEN]
        return out

class Net(nn.Module):
    def __init__(self, width=64):
        super(Net, self).__init__()
        self.kernel_size = (2, 3)
        self.pad1 = nn.ConstantPad2d((1, 1, 0, 0), value=0.)
        self.width = width

        self.enc_conv1 = nn.Conv2d(in_channels=self.width//2, out_channels=self.width, kernel_size=(1, 3), stride=(1, 2))  # [b, 64, nframes, 256]
        self.enc_snorm1 = SwitchNorm2d(64)
        self.enc_prelu1 = nn.PReLU(self.width)

        self.dual_transformer = Dual_Transformer(64, 64, num_layers=4)  # # [b, 64, nframes, 8]

        self.encoder = Encoder()

        self.mask = Mask()

        self.decoder = Decoder()

        #init
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_normal_(p)


    def forward(self, input):

        out = self.encoder(input)

        x1 = self.enc_prelu1(self.enc_snorm1(self.enc_conv1(self.pad1(out))))  # [b, 64, T, F/2]   #先归一化再激活

        out = self.dual_transformer(x1)  # [b, 64, T, 100]

        out = self.mask(x1, out)

        out = self.decoder(out)

        return out





if __name__ == '__main__':
    import os
    from thop import profile
    os.environ['CUDA_VISIBLE_DEVICES'] = '1'

    x = torch.randn(1, 1, 16000)
    model = Net()
    out = model(x)
    print(out.shape)


    # 计算FLOPs
    flops, params = profile(model, inputs=(x,), verbose=False)
    print(f"FLOPs: {flops / 1e9:.2f}G, Params: {params / 1024 ** 2:.2f}MB")

