# Copyright (c) Chris Choy (chrischoy@ai.stanford.edu).
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Please cite "4D Spatio-Temporal ConvNets: Minkowski Convolutional Neural
# Networks", CVPR'19 (https://arxiv.org/abs/1904.08755) if you use any part
# of the code.
import os
import argparse
import numpy as np
from urllib.request import urlretrieve

try:
    import open3d as o3d
except ImportError:
    raise ImportError('Please install open3d with `pip install open3d`.')

import torch
import MinkowskiEngine as ME
# from examples.minkunet import MinkUNet34C
from minkunet import MinkUNet34C

from thop import profile, clever_format

# Check if the weights and file exist and download
if not os.path.isfile('weights.pth'):
    print('Downloading weights...')
    urlretrieve("https://bit.ly/2O4dZrz", "weights.pth")
if not os.path.isfile("1.ply"):
    print('Downloading an example pointcloud...')
    urlretrieve("https://bit.ly/3c2iLhg", "1.ply")


parser = argparse.ArgumentParser()
parser.add_argument('--file_name', type=str, default='1.ply')
parser.add_argument('--weights', type=str, default='weights.pth')
parser.add_argument('--use_cpu', action='store_true')


def load_file(file_name):
    pcd = o3d.io.read_point_cloud(file_name)
    coords = np.array(pcd.points)
    colors = np.array(pcd.colors)
    return coords, colors, pcd


def normalize_color(color: torch.Tensor, is_color_in_range_0_255: bool = False) -> torch.Tensor:
    r"""
    Convert color in range [0, 1] to [-0.5, 0.5]. If the color is in range [0,
    255], use the argument `is_color_in_range_0_255=True`.

    `color` (torch.Tensor): Nx3 color feature matrix
    `is_color_in_range_0_255` (bool): If the color is in range [0, 255] not [0, 1], normalize the color to [0, 1].
    """
    if is_color_in_range_0_255:
        color /= 255
    color -= 0.5
    return color.float()


if __name__ == '__main__':
    config = parser.parse_args()
    device = torch.device('cuda' if (
        torch.cuda.is_available() and not config.use_cpu) else 'cpu')
    print(f"Using {device}")
    # Define a model and load the weights
    model = MinkUNet34C(3, 20).to(device)

    coords, colors, pcd = load_file(config.file_name)
    print(f'coords shape: {coords.shape} \n coords: {coords}')
    print(f'colors shape: {colors.shape} \n colors: {colors}')

    voxel_size = 0.02
    
    in_field = ME.TensorField(
        features=normalize_color(torch.from_numpy(colors)),
        coordinates=ME.utils.batched_coordinates([coords / voxel_size], dtype=torch.float32),
        quantization_mode=ME.SparseTensorQuantizationMode.UNWEIGHTED_AVERAGE,
        minkowski_algorithm=ME.MinkowskiAlgorithm.SPEED_OPTIMIZED,
        device=device,
    )
        
    # Convert to a sparse tensor
    sinput = in_field.sparse()
    # Calculate the FLOPs and Params of the model
    flops, params = profile(model, inputs=(sinput, ))
    print(f'flops: {flops}, params: {params}')
    flops, params = clever_format([flops, params],"%.3f")
    print(f'flops: {flops}, params: {params}')
