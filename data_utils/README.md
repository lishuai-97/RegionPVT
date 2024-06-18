# S3DIS

**Generate standford_indoor3d dataset**

Modefied from https://github.com/yanx27/Pointnet_Pointnet2_pytorch for S3DIS preprocessing.

```bash
python collect_indoor3d_data.py
```

Processed data will save in `output_folder='data/stanford_indoor3d'`


# ScanNetv2

**Download ScanNetv2 dataset**

Modefied from https://github.com/dvlab-research/PointGroup for ScanNetv2 preprocessing.

```bash
cd scannetv2
python download_scannetv2.py -o scannetv2/ --type  _vh_clean_2.ply
python download_scannetv2.py -o scannetv2/ --type  _vh_clean_2.labels.ply
python download_scannetv2.py -o scannetv2/ --type  _vh_clean_2.0.010000.segs.json
python download_scannetv2.py -o scannetv2/ --type  .aggregation.json
```

Downloaded data will be saved in `./scannet` folder.

**Generate input files `[scene_id]_inst_nostuff.pth` for `train/val/test`**

```bash
python prepare_data_inst.py --data_split train
python prepare_data_inst.py --data_split val
python prepare_data_inst.py --data_split test
```