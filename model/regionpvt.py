import torch
import torch.nn as nn
from torch_points3d.modules.KPConv.kernels import KPConvLayer
from torch_scatter import scatter_softmax
from timm.models.layers import DropPath, trunc_normal_
from torch_points3d.core.common_modules import FastBatchNorm1d
from torch_geometric.nn import voxel_grid
from libs.pointops2.functions import pointops

import torch.nn.functional as F
import MinkowskiEngine as ME

from model.transformer_base import LocalSelfAttentionBase
from model.common import stride_centroids, downsample_points, downsample_embeddings
import libs.cuda_ops.functions.sparse_ops as ops


def grid_sample(pos, batch, size, start, return_p2v=True):
    # pos: float [N, 3]
    # batch: long [N]
    # size: float [3, ]
    # start: float [3, ] / None

    cluster = voxel_grid(pos, batch, size, start=start) #[N, ]

    if return_p2v == False:
        unique, cluster = torch.unique(cluster, sorted=True, return_inverse=True)
        return cluster

    unique, cluster, counts = torch.unique(cluster, sorted=True, return_inverse=True, return_counts=True)

    # obtain p2v_map
    n = unique.shape[0]
    k = counts.max().item()
    p2v_map = cluster.new_zeros(n, k) #[n, k]
    mask = torch.arange(k).cuda().unsqueeze(0) < counts.unsqueeze(-1) #[n, k]
    p2v_map[mask] = torch.argsort(cluster)

    return cluster, p2v_map, counts


class Mlp(nn.Module):
    """ Multilayer perceptron."""

    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop, inplace=True)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class TransitionDown(nn.Module):
    def __init__(self, in_channels, out_channels, ratio, k, norm_layer=nn.LayerNorm):
        super().__init__()
        self.ratio = ratio
        self.k = k
        self.norm = norm_layer(in_channels) if norm_layer else None
        self.linear = nn.Linear(in_channels, out_channels, bias=False)
        self.pool = nn.MaxPool1d(k)

    def forward(self, feats, xyz, offset):

        n_offset, count = [int(offset[0].item()*self.ratio)+1], int(offset[0].item()*self.ratio)+1
        for i in range(1, offset.shape[0]):
            count += ((offset[i].item() - offset[i-1].item())*self.ratio) + 1
            n_offset.append(count)
        n_offset = torch.cuda.IntTensor(n_offset)
        idx = pointops.furthestsampling(xyz, offset, n_offset)  # (m)
        n_xyz = xyz[idx.long(), :]  # (m, 3)

        feats = pointops.queryandgroup(self.k, xyz, n_xyz, feats, None, offset, n_offset, use_xyz=False)  # (m, nsample, 3+c)
        m, k, c = feats.shape
        feats = self.linear(self.norm(feats.view(m*k, c)).view(m, k, c)).transpose(1, 2).contiguous()
        feats = self.pool(feats).squeeze(-1)  # (m, c)
        
        return feats, n_xyz, n_offset


####################################
# Local Window Attention Layer
####################################
class WindowAttention(nn.Module):
    """ Window based multi-head self attention (W-MSA) module with relative position bias.
    It supports both of shifted and non-shifted window.

    Args:
        dim (int): Number of input channels.
        window_size (tuple[int]): The height and width of the window.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set
        attn_drop (float, optional): Dropout ratio of attention weight. Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
    """

    def __init__(self, dim, window_size, num_heads, quant_size, rel_query=True, rel_key=False, rel_value=False, qkv_bias=True, qk_scale=None, attn_drop=0., proj_drop=0.):

        super().__init__()
        self.dim = dim
        self.window_size = window_size
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.quant_size = quant_size
        self.rel_query = rel_query
        self.rel_key = rel_key
        self.rel_value = rel_value

        quant_grid_length = int(window_size / quant_size)
        if rel_query:
            self.relative_pos_query_table = nn.Parameter(torch.zeros(2*quant_grid_length-1, num_heads, head_dim, 3))
            trunc_normal_(self.relative_pos_query_table, std=.02)
        if rel_key:
            self.relative_pos_key_table = nn.Parameter(torch.zeros(2*quant_grid_length-1, num_heads, head_dim, 3))
            trunc_normal_(self.relative_pos_key_table, std=.02)
        if rel_value:
            self.relative_pos_value_table = nn.Parameter(torch.zeros(2*quant_grid_length-1, num_heads, head_dim, 3))
            trunc_normal_(self.relative_pos_value_table, std=.02)

        self.quant_grid_length = quant_grid_length

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop, inplace=True)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop, inplace=True)

        self.softmax = nn.Softmax(dim=-1)

    def map_func(self, relative_position):
        return relative_position + self.quant_grid_length - 1

    def forward(self, feats, xyz, index_0, index_0_offsets, n_max, index_1, shift_size):
        """ Forward function.

        Args:
            feats: N, C
            xyz: N, 3
            p2v_idx: n, k
            counts: n, 
        """

        N, C = feats.shape
        
        # Query, Key, Value
        qkv = self.qkv(feats).reshape(N, 3, self.num_heads, C // self.num_heads).permute(1, 0, 2, 3).contiguous()
        query, key, value = qkv[0], qkv[1], qkv[2] #[N, num_heads, C//num_heads]
        query = query * self.scale
        
        attn_flat = pointops.attention_step1_v2(query.float(), key.float(), index_1.int(), index_0_offsets.int(), n_max)

        xyz_quant = (xyz - xyz.min(0)[0] + shift_size) % self.window_size
        xyz_quant = xyz_quant // self.quant_size #[N, 3]
        relative_position = xyz_quant[index_0] - xyz_quant[index_1] #[M, 3]
        relative_position_index = self.map_func(relative_position) #[M, 3]
        
        if self.rel_query and self.rel_key:
            relative_position_bias = pointops.dot_prod_with_idx_v3(query.float(), index_0_offsets.int(), n_max, key.float(), index_1.int(), self.relative_pos_query_table.float(), self.relative_pos_key_table.float(), relative_position_index.int())
        elif self.rel_query:
            relative_position_bias = pointops.dot_prod_with_idx(query.float(), index_0.int(), self.relative_pos_query_table.float(), relative_position_index.int()) #[M, num_heads]
        elif self.rel_key:
            relative_position_bias = pointops.dot_prod_with_idx(key.float(), index_1.int(), self.relative_pos_key_table.float(), relative_position_index.int()) #[M, num_heads]
        else:
            relative_position_bias = 0
            
        attn_flat = attn_flat + relative_position_bias #[M, num_heads]
        
        softmax_attn_flat = scatter_softmax(src=attn_flat, index=index_0, dim=0) #[M, num_heads]

        if self.rel_value:
            x = pointops.attention_step2_with_rel_pos_value_v2(softmax_attn_flat.float(), value.float(), index_0_offsets.int(), n_max, index_1.int(), self.relative_pos_value_table.float(), relative_position_index.int())
        else:
            x = pointops.attention_step2(softmax_attn_flat.float(), value.float(), index_0.int(), index_1.int())
        x = x.view(N, C)

        x = self.proj(x)
        x = self.proj_drop(x) #[N, C]

        return x


####################################
# Regional Attention Layer
####################################
class LightweightSelfAttentionLayer(LocalSelfAttentionBase):
    def __init__(
        self,
        in_channels,
        out_channels=None,
        kernel_size=3,
        stride=1,
        dilation=1,
        num_heads=8,
    ):
        out_channels = in_channels if out_channels is None else out_channels
        assert out_channels % num_heads == 0
        assert kernel_size % 2 == 1
        assert stride == 1, "Currently, this layer only supports stride == 1"
        assert dilation == 1, "Currently, this layer only supports dilation == 1"
        super(LightweightSelfAttentionLayer, self).__init__(kernel_size, stride, dilation, dimension=3)

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.dilation = dilation
        self.num_heads = num_heads
        self.attn_channels = out_channels // num_heads

        self.to_query = nn.Sequential(
            ME.MinkowskiLinear(in_channels, out_channels),
            ME.MinkowskiToFeature()
        )
        self.to_value = nn.Sequential(
            ME.MinkowskiLinear(in_channels, out_channels),
            ME.MinkowskiToFeature()
        )
        self.to_out = nn.Linear(out_channels, out_channels)

        self.inter_pos_enc = nn.Parameter(torch.FloatTensor(self.kernel_volume, self.num_heads, self.attn_channels))
        self.intra_pos_mlp = nn.Sequential(
            nn.Linear(3, 3, bias=False),
            nn.BatchNorm1d(3),
            nn.ReLU(inplace=True),
            nn.Linear(3, in_channels, bias=False),
            nn.BatchNorm1d(in_channels),
            nn.ReLU(inplace=True),
            nn.Linear(in_channels, in_channels)
        )
        nn.init.normal_(self.inter_pos_enc, 0, 1)

    def forward(self, stensor, norm_points):
        dtype = stensor._F.dtype
        device = stensor._F.device

        # query, key, value, and relative positional encoding
        intra_pos_enc = self.intra_pos_mlp(norm_points)
        stensor = stensor + intra_pos_enc
        q = self.to_query(stensor).view(-1, self.num_heads, self.attn_channels).contiguous()
        v = self.to_value(stensor).view(-1, self.num_heads, self.attn_channels).contiguous()
        q = torch.as_tensor(q, dtype=dtype)
        v = torch.as_tensor(v, dtype=dtype)

        # key-query map
        kernel_map, out_key = self.get_kernel_map_and_out_key(stensor)
        kq_map = self.key_query_map_from_kernel_map(kernel_map)

        # attention weights with cosine similarity
        attn = torch.zeros((kq_map.shape[1], self.num_heads), dtype=dtype, device=device)
        norm_q = F.normalize(q, p=2, dim=-1)
        norm_pos_enc = F.normalize(self.inter_pos_enc, p=2, dim=-1)
        attn = ops.dot_product_cuda(norm_q, norm_pos_enc, attn, kq_map)

        # aggregation & the output
        out_F = torch.zeros((len(q), self.num_heads, self.attn_channels),
                            dtype=dtype,
                            device=device)
        kq_indices = self.key_query_indices_from_key_query_map(kq_map)
        out_F = ops.scalar_attention_cuda(attn, v, out_F, kq_indices)
        out_F = self.to_out(out_F.view(-1, self.out_channels).contiguous())
        return ME.SparseTensor(out_F,
                               coordinate_map_key=out_key,
                               coordinate_manager=stensor.coordinate_manager)


class R2LEncoderBlock(nn.Module):
    r""" Regional-to-Local (R2L) Point-Voxel Transformer Encoder Block.

    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads.
        window_size (float): Window size.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set.
        drop_path (float, optional): Stochastic depth rate. Default: 0.0
        act_layer (nn.Module, optional): Activation layer. Default: nn.GELU
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm
    """
    QMODE = ME.SparseTensorQuantizationMode.UNWEIGHTED_AVERAGE

    def __init__(self, dim, num_heads, window_size, quant_size,
            rel_query=True, rel_key=False, rel_value=False, drop_path=0.0, \
            mlp_ratio=4.0, qkv_bias=True, qk_scale=None, act_alyer=nn.GELU, norm_layer=nn.LayerNorm, mode=4):    # mode=4:mean
        super().__init__()

        self.window_size = window_size
        self.mode = mode

        self.norm1 = norm_layer(dim)
        self.local_attn = WindowAttention(dim, window_size=self.window_size, num_heads=num_heads, quant_size=quant_size,
            rel_query=rel_query, rel_key=rel_key, rel_value=rel_value, qkv_bias=qkv_bias, qk_scale=qk_scale)
        
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_alyer)

        self.regional_attn = LightweightSelfAttentionLayer(in_channels=dim, out_channels=dim, num_heads=num_heads)
        self.bn = ME.MinkowskiBatchNorm(dim)
        self.relu = ME.MinkowskiReLU(inplace=True)

        self.ENC_DIM = dim
        self.enc_mlp = nn.Sequential(
            nn.Linear(3, self.ENC_DIM, bias=False),
            nn.BatchNorm1d(self.ENC_DIM),
            nn.Tanh(),
            nn.Linear(self.ENC_DIM, self.ENC_DIM, bias=False),
            nn.BatchNorm1d(self.ENC_DIM),
            nn.Tanh()
        )


    @torch.no_grad()
    def normalize_points(self, points, centroids, tensor_map):
        tensor_map = tensor_map if tensor_map.dtype == torch.int64 else tensor_map.long()
        norm_points = points - centroids[tensor_map]
        return norm_points


    @torch.no_grad()
    def normalize_centroids(self, down_points, coordinates, tensor_stride):
        norm_points = (down_points - coordinates[:, 1:]) / tensor_stride - 0.5
        return norm_points


    def voxelize_with_centroids(self, x: ME.TensorField):
        cm = x.coordinate_manager
        points = x.C[:, 1:]

        out = x.sparse()
        size = torch.Size([len(out), len(x)])
        tensor_map, field_map = cm.field_to_sparse_map(x.coordinate_key, out.coordinate_key)    # tensor_map: v2p_map as "cluster" in the function grid_sample of stratified transformer
        points_p1, count_p1 = downsample_points(points, tensor_map, field_map, size)            # centroid coordinates of voxels
        norm_points = self.normalize_points(points, points_p1, tensor_map)

        pos_embs = self.enc_mlp(norm_points)
        pos_embs = torch.as_tensor(pos_embs, dtype=torch.float32)
        down_pos_embs = downsample_embeddings(pos_embs, tensor_map, size, mode="avg")
        out_F = out.F + down_pos_embs
        out = ME.SparseTensor(out_F,
                            coordinate_map_key=out.coordinate_key,
                            coordinate_manager=cm)

        norm_points_p1 = self.normalize_centroids(points_p1, out.C, out.tensor_stride[0])
        return out, norm_points_p1, points_p1, count_p1, pos_embs

    
    def forward(self, feats, xyz, offset):
        """ Forward function.
        
        Args:
            feats: N, C
            xyz: N, 3
            offset: N
            window_size (float): Window size
        """
        xyz_ = xyz / self.window_size
        batch_coordinates = torch.cat([offset.unsqueeze(-1), xyz_], dim=1)      # [N, 4]
        batch_coordinates_ = torch.as_tensor(batch_coordinates, dtype=torch.float32)
        feats_ = torch.as_tensor(feats, dtype=torch.float32)

        in_data = ME.TensorField(
            features=feats_,
            coordinates=batch_coordinates_,
            quantization_mode=self.QMODE
        )

        # RSA:: y_r = ReLU(BN(RSA(x_r)))
        out, norm_points_p1, _, _, _ = self.voxelize_with_centroids(in_data)
        regional_tokens = self.relu(self.bn(self.regional_attn(out, norm_points_p1)))

        # CAT:: y = x_l || y_r
        voxel_point_coordinates = torch.cat([in_data.coordinates, regional_tokens.coordinates], dim=0)
        voxel_point_features = torch.cat([in_data.features, regional_tokens.features], dim=0)

        coords_dim = voxel_point_coordinates.shape[1]
        feats_dim = voxel_point_features.shape[1]
        voxel_point = torch.cat([voxel_point_coordinates, voxel_point_features], dim=1)
        # torch.cat()以后的batch是杂乱的(in_data's batch, regional_tokens's batch)，需要对voxel_point按照batch大小重新进行排序，并获取排序后的索引
        _, indices_ = torch.sort(voxel_point[:, 0])
        batch_voxel_point = voxel_point[indices_]
        batch_voxel_point_coordinates, batch_voxel_point_features = torch.split(batch_voxel_point, [coords_dim, feats_dim], dim=1)
        batch_voxel_point_coordinates_, batch_voxel_point_features_ = batch_voxel_point_coordinates.contiguous(), batch_voxel_point_features.contiguous()

        xyz_new = batch_voxel_point_coordinates_[:, 1:] * self.window_size
        xyz_new = xyz_new.type_as(xyz).to(xyz.device)
        feats_new = batch_voxel_point_features_.type_as(feats).to(feats.device)
        window_size = torch.tensor([self.window_size]*3).type_as(xyz).to(xyz.device)
        batch_new = batch_voxel_point_coordinates_[:, 0].long()

        # region
        # obtain p2v_map
        v2p_map, p2v_map, counts = grid_sample(xyz_new, batch_new, window_size, start=None)
        
        # pre-compute all paired index of query and key that need to perform dot product
        N, C = feats_new.shape
        n, k = p2v_map.shape
        mask = torch.arange(k).unsqueeze(0).cuda() < counts.unsqueeze(-1)   # [n, k]
        mask_mat = (mask.unsqueeze(-1) & mask.unsqueeze(-2))                # [n, k, k]
        index_0 = p2v_map.unsqueeze(-1).expand(-1, -1, k)[mask_mat]         # [M, ]
        index_1 = p2v_map.unsqueeze(1).expand(-1, k, -1)[mask_mat]          # [M, ]
        M = index_0.shape[0]

        # rearrange index for acceleration
        index_0, indices = torch.sort(index_0) #[M,]
        index_1 = index_1[indices] #[M,]
        index_0_counts = index_0.bincount()
        n_max = index_0_counts.max()
        index_0_offsets = index_0_counts.cumsum(dim=-1) #[N]
        index_0_offsets = torch.cat([torch.zeros(1, dtype=torch.long).cuda(), index_0_offsets], 0) #[N+1]

        assert index_0.shape[0] == index_1.shape[0]
        assert index_0.shape[0] == (counts ** 2).sum()

        shift_size = 0
        # endregion

        # LSA:: z = y + LSA(LN(y))
        short_cut = feats_new
        feats = self.norm1(feats_new)
        feats = self.local_attn(feats, xyz_new, index_0, index_0_offsets, n_max, index_1, shift_size)    # [N, c]

        feats = short_cut + self.drop_path(feats)
        feats = feats + self.drop_path(self.mlp(self.norm2(feats)))

        batch_voxel_point_new = torch.cat([xyz_new, feats], dim=1)
        # 变回原来的 point || voxel 的顺序
        rearranged_batch_voxel_point = batch_voxel_point_new[torch.argsort(indices_)]
        rearranged_batch_voxel_point_coordinates, rearranged_batch_voxel_point_features = torch.split(rearranged_batch_voxel_point, [xyz_new.shape[1], feats.shape[1]], dim=1)

        rearranged_in_data_coordinates, rearranged_regional_tokens_coordinates = torch.split(rearranged_batch_voxel_point_coordinates, [in_data.coordinates.shape[0], regional_tokens.coordinates.shape[0]], dim=0)
        rearranged_Local_point_features, rearrange_regional_tokens_features = torch.split(rearranged_batch_voxel_point_features, [in_data.features.shape[0], regional_tokens.features.shape[0]], dim=0)

        # assert rearranged_in_data_coordinates == in_data.coordinates[:, 1:]
        # assert rearranged_regional_tokens_coordinates == regional_tokens.coordinates

        feats = rearranged_Local_point_features.contiguous()

        return feats

class BasicLayer(nn.Module):
    def __init__(self, depth, channel, num_heads, window_size, grid_size, quant_size, 
            rel_query=True, rel_key=False, rel_value=False, drop_path=0.0, mlp_ratio=4.0, qkv_bias=True, \
            qk_scale=None, norm_layer=nn.LayerNorm, downsample=None, ratio=0.25, k=16, out_channels=None):
        super().__init__()
        self.window_size = window_size
        self.depth = depth
        self.grid_size = grid_size

        self.blocks = nn.ModuleList([R2LEncoderBlock(channel, num_heads, window_size, quant_size, 
            rel_query=rel_query, rel_key=rel_key, rel_value=rel_value, drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path,\
            mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, norm_layer=norm_layer) for i in range(depth)])

        self.downsample = downsample(channel, out_channels, ratio, k) if downsample else None

    def forward(self, feats, xyz, offset):
        # feats: N, C
        # xyz: N, 3
        # offset: [batch_size]
        
        offset_ = offset.clone()
        offset_[1:] = offset_[1:] - offset_[:-1]
        batch = torch.cat([torch.tensor([ii]*o) for ii, o in enumerate(offset_)], 0).long().cuda()

        for i, blk in enumerate(self.blocks):
            feats = blk(feats, xyz, batch) #[N, C]

        if self.downsample:
            feats_down, xyz_down, offset_down = self.downsample(feats, xyz, offset)
        else:
            feats_down, xyz_down, offset_down = None, None, None
            
        return feats, xyz, offset, feats_down, xyz_down, offset_down


class Upsample(nn.Module):
    def __init__(self, k, in_channels, out_channels, bn_momentum=0.02):
        super().__init__()
        self.k = k
        self.in_channels = in_channels
        self.out_channels = out_channels

        self.linear1 = nn.Sequential(nn.LayerNorm(out_channels), nn.Linear(out_channels, out_channels))
        self.linear2 = nn.Sequential(nn.LayerNorm(in_channels), nn.Linear(in_channels, out_channels))

    def forward(self, feats, xyz, support_xyz, offset, support_offset, support_feats=None):

        feats = self.linear1(support_feats) + pointops.interpolation(xyz, support_xyz, self.linear2(feats), offset, support_offset)
        return feats, support_xyz, support_offset


class KPConvSimpleBlock(nn.Module):
    def __init__(self, in_channels, out_channels, prev_grid_size, sigma=1.0, negative_slope=0.2, bn_momentum=0.02):
        super().__init__()
        self.kpconv = KPConvLayer(in_channels, out_channels, point_influence=prev_grid_size * sigma, add_one=False)
        self.bn = FastBatchNorm1d(out_channels, momentum=bn_momentum)
        self.activation = nn.LeakyReLU(negative_slope=negative_slope)

    def forward(self, feats, xyz, batch, neighbor_idx):
        # feats: [N, C]
        # xyz: [N, 3]
        # batch: [N,]
        # neighbor_idx: [N, M]

        feats = self.kpconv(xyz, xyz, neighbor_idx, feats)
        feats = self.activation(self.bn(feats))
        return feats


class KPConvResBlock(nn.Module):
    def __init__(self, in_channels, out_channels, prev_grid_size, sigma=1.0, negative_slope=0.2, bn_momentum=0.02):
        super().__init__()
        d_2 = out_channels // 4
        activation = nn.LeakyReLU(negative_slope=negative_slope)
        self.unary_1 = torch.nn.Sequential(nn.Linear(in_channels, d_2, bias=False), FastBatchNorm1d(d_2, momentum=bn_momentum), activation)
        self.unary_2 = torch.nn.Sequential(nn.Linear(d_2, out_channels, bias=False), FastBatchNorm1d(out_channels, momentum=bn_momentum), activation)
        self.kpconv = KPConvLayer(d_2, d_2, point_influence=prev_grid_size * sigma, add_one=False)
        self.bn = FastBatchNorm1d(out_channels, momentum=bn_momentum)
        self.activation = activation

        if in_channels != out_channels:
            self.shortcut_op = torch.nn.Sequential(
                nn.Linear(in_channels, out_channels, bias=False), FastBatchNorm1d(out_channels, momentum=bn_momentum)
            )
        else:
            self.shortcut_op = nn.Identity()

    def forward(self, feats, xyz, batch, neighbor_idx):
        # feats: [N, C]
        # xyz: [N, 3]
        # batch: [N,]
        # neighbor_idx: [N, M]
        
        shortcut = feats
        feats = self.unary_1(feats)
        feats = self.kpconv(xyz, xyz, neighbor_idx, feats)
        feats = self.unary_2(feats)
        shortcut = self.shortcut_op(shortcut)
        feats += shortcut
        return feats


class RegionPVT(nn.Module):
    def __init__(self, depths, channels, num_heads, window_sizes, up_k, \
            grid_sizes, quant_sizes, rel_query=True, rel_key=False, rel_value=False, drop_path_rate=0.2, \
            num_layers=4, concat_xyz=False, num_classes=13, ratio=0.25, k=16, prev_grid_size=0.04, sigma=1.0, stem_transformer=False):
        super().__init__()
        
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule

        if stem_transformer:
            self.stem_layer = nn.ModuleList([
                KPConvSimpleBlock(3 if not concat_xyz else 6, channels[0], prev_grid_size, sigma=sigma)
            ])
            self.layer_start = 0
        else:
            self.stem_layer = nn.ModuleList([
                KPConvSimpleBlock(3 if not concat_xyz else 6, channels[0], prev_grid_size, sigma=sigma),
                KPConvResBlock(channels[0], channels[0], prev_grid_size, sigma=sigma)
            ])
            self.downsample = TransitionDown(channels[0], channels[1], ratio, k)
            self.layer_start = 1

        self.layers = nn.ModuleList([BasicLayer(depths[i], channels[i], num_heads[i], window_sizes[i], grid_sizes[i], \
            quant_sizes[i], rel_query=rel_query, rel_key=rel_key, rel_value=rel_value, \
            drop_path=dpr[sum(depths[:i]):sum(depths[:i+1])], downsample=TransitionDown if i < num_layers-1 else None, \
            ratio=ratio, k=k, out_channels=channels[i+1] if i < num_layers-1 else None) for i in range(self.layer_start, num_layers)])

        self.upsamples = nn.ModuleList([Upsample(up_k, channels[i], channels[i-1]) for i in range(num_layers-1, 0, -1)])
        
        self.classifier = nn.Sequential(
            nn.Linear(channels[0], channels[0]), 
            nn.BatchNorm1d(channels[0]), 
            nn.ReLU(inplace=True), 
            nn.Linear(channels[0], num_classes)
        )

        self.init_weights()

    def forward(self, feats, xyz, offset, batch, neighbor_idx):

        feats_stack = []
        xyz_stack = []
        offset_stack = []

        for i, layer in enumerate(self.stem_layer):
            feats = layer(feats, xyz, batch, neighbor_idx)

        feats = feats.contiguous()
        
        if self.layer_start == 1:
            feats_stack.append(feats)
            xyz_stack.append(xyz)
            offset_stack.append(offset)
            feats, xyz, offset = self.downsample(feats, xyz, offset)

        for i, layer in enumerate(self.layers):
            feats, xyz, offset, feats_down, xyz_down, offset_down = layer(feats, xyz, offset)

            feats_stack.append(feats)
            xyz_stack.append(xyz)
            offset_stack.append(offset)

            feats = feats_down
            xyz = xyz_down
            offset = offset_down

        feats = feats_stack.pop()
        xyz = xyz_stack.pop()
        offset = offset_stack.pop()

        for i, upsample in enumerate(self.upsamples):
            feats, xyz, offset = upsample(feats, xyz, xyz_stack.pop(), offset, offset_stack.pop(), support_feats=feats_stack.pop())

        out = self.classifier(feats)

        return out        

    def init_weights(self):
        """Initialize the weights in backbone.
        """

        def _init_weights(m):
            if isinstance(m, nn.Linear):
                trunc_normal_(m.weight, std=.02)
                if isinstance(m, nn.Linear) and m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.LayerNorm) or isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.bias, 0)
                nn.init.constant_(m.weight, 1.0)

        self.apply(_init_weights)