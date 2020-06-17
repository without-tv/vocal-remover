import os
import random

import numpy as np
import sklearn
import torch
import torch.utils.data
from tqdm import tqdm

from lib import spec_utils


class VocalRemoverValidationSet(torch.utils.data.Dataset):

    def __init__(self, filelist):
        self.filelist = filelist
        self.label_bins = np.linspace(-np.pi, np.pi + 1e-6, num=16)

    def __len__(self):
        return len(self.filelist)

    def __getitem__(self, idx):
        path = self.filelist[idx]
        data = np.load(path)
        X, y = data['X'], data['y']

        X_mag = np.abs(X)
        X_phase = np.angle(X)

        y_mag = np.abs(y)
        y_phase = np.digitize(np.angle(y), self.label_bins)

        return (X_mag, X_phase), (y_mag, y_phase)


def make_pair(mix_dir, inst_dir):
    input_exts = ['.wav', '.m4a', '.3gp', '.oma', '.mp3', '.mp4']

    X_list = sorted([
        os.path.join(mix_dir, fname)
        for fname in os.listdir(mix_dir)
        if os.path.splitext(fname)[1] in input_exts])
    y_list = sorted([
        os.path.join(inst_dir, fname)
        for fname in os.listdir(inst_dir)
        if os.path.splitext(fname)[1] in input_exts])

    filelist = list(zip(X_list, y_list))

    return filelist


def train_val_split(mix_dir, inst_dir, val_rate, val_filelist):
    filelist = make_pair(mix_dir, inst_dir)
    random.shuffle(filelist)

    if len(val_filelist) == 0:
        val_size = int(len(filelist) * val_rate)
        train_filelist = filelist[:-val_size]
        val_filelist = filelist[-val_size:]
    else:
        train_filelist = [
            pair for pair in filelist
            if list(pair) not in val_filelist]

    return train_filelist, val_filelist


def mixup_generator(X, y, rate, alpha):
    perm = np.random.permutation(len(X))[:int(len(X) * rate)]
    for i in range(len(perm) - 1):
        lam = np.random.beta(alpha, alpha)
        X[perm[i]] = lam * X[perm[i]] + (1 - lam) * X[perm[i + 1]]
        y[perm[i]] = lam * y[perm[i]] + (1 - lam) * y[perm[i + 1]]

    return X, y


def get_oracle_data(X, y, instance_loss, oracle_rate, oracle_drop_rate):
    k = int(len(X) * oracle_rate * (1 / (1 - oracle_drop_rate)))
    n = int(len(X) * oracle_rate)
    idx = np.argsort(instance_loss)[::-1][:k]
    idx = np.random.choice(idx, n, replace=False)
    oracle_X = X[idx].copy()
    oracle_y = y[idx].copy()

    return oracle_X, oracle_y, idx


def make_padding(width, cropsize, offset):
    left = offset
    roi_size = cropsize - left * 2
    if roi_size == 0:
        roi_size = cropsize
    right = roi_size - (width % roi_size) + left

    return left, right, roi_size


def get_statistics(filelist, sr, hop_length, n_fft):
    scaler = sklearn.preprocessing.StandardScaler()

    for X_path, y_path in tqdm(filelist):
        X, y = spec_utils.cache_or_load(X_path, y_path, sr, hop_length, n_fft)
        scaler.partial_fit(np.squeeze(np.abs(X).mean(axis=0)).T)

    return scaler.mean_, scaler.scale_


def make_training_set(filelist, cropsize, patches, sr, hop_length, n_fft, offset):
    len_dataset = patches * len(filelist)

    X_dataset = np.zeros(
        (len_dataset, 2, n_fft // 2 + 1, cropsize), dtype=np.complex64)
    y_dataset = np.zeros(
        (len_dataset, 2, n_fft // 2 + 1, cropsize), dtype=np.complex64)

    for i, (X_path, y_path) in enumerate(tqdm(filelist)):
        p = np.random.uniform()
        if p < 0.1:
            X_path.replace(os.path.splitext(X_path)[1], '_pitch-1.wav')
            y_path.replace(os.path.splitext(y_path)[1], '_pitch-1.wav')
        elif p < 0.2:
            X_path.replace(os.path.splitext(X_path)[1], '_pitch1.wav')
            y_path.replace(os.path.splitext(y_path)[1], '_pitch1.wav')

        X, y = spec_utils.cache_or_load(X_path, y_path, sr, hop_length, n_fft)

        l, r, roi_size = make_padding(X.shape[2], cropsize, offset)
        X_pad = np.pad(X, ((0, 0), (0, 0), (l, r)), mode='constant')
        y_pad = np.pad(y, ((0, 0), (0, 0), (l, r)), mode='constant')

        starts = np.random.randint(0, X_pad.shape[2] - cropsize, patches)
        ends = starts + cropsize
        for j in range(patches):
            idx = i * patches + j
            X_dataset[idx] = X_pad[:, :, starts[j]:ends[j]]
            y_dataset[idx] = y_pad[:, :, starts[j]:ends[j]]
            if np.random.uniform() < 0.5:
                # swap channel
                X_dataset[idx] = X_dataset[idx, ::-1]
                y_dataset[idx] = y_dataset[idx, ::-1]

    return X_dataset, y_dataset


def make_validation_set(filelist, cropsize, sr, hop_length, n_fft, offset):
    patch_list = []
    outdir = 'cs{}_sr{}_hl{}_nf{}_of{}'.format(cropsize, sr, hop_length, n_fft, offset)
    os.makedirs(outdir, exist_ok=True)

    for i, (X_path, y_path) in enumerate(tqdm(filelist)):
        basename = os.path.splitext(os.path.basename(X_path))[0]

        X, y = spec_utils.cache_or_load(X_path, y_path, sr, hop_length, n_fft)

        l, r, roi_size = make_padding(X.shape[2], cropsize, offset)
        X_pad = np.pad(X, ((0, 0), (0, 0), (l, r)), mode='constant')
        y_pad = np.pad(y, ((0, 0), (0, 0), (l, r)), mode='constant')

        len_dataset = int(np.ceil(X.shape[2] / roi_size))
        for j in range(len_dataset):
            outpath = os.path.join(outdir, '{}_p{}.npz'.format(basename, j))
            start = j * roi_size
            if not os.path.exists(outpath):
                np.savez(
                    outpath,
                    X=X_pad[:, :, start:start + cropsize],
                    y=y_pad[:, :, start:start + cropsize])
            patch_list.append(outpath)

    return VocalRemoverValidationSet(patch_list)
