# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
from functools import partial

from collections import Counter, OrderedDict
import pickle
import json
import multiprocessing as mp

import numpy as np

from absl import flags
import tensorflow as tf
from vocabulary import Vocab

from tensorflow.python.platform.gfile import Exists as exists
from tensorflow.python.platform.gfile import MakeDirs as makedirs
from tensorflow.python.platform.gfile import Glob as glob


def _preprocess(shard, train, vocab, save_dir, cutoffs, bin_sizes, bsz, tgt_len,
                num_core_per_host, use_tpu, num_shuffle):
    file_names = []
    num_batch = 0

    path = train[shard]
    data_shard = vocab.encode_file(path, ordered=False, add_double_eos=True)

    for shuffle in range(num_shuffle):
        basename = "train-{:03d}-{:02d}".format(shard, shuffle)
        print("Processing shard {} shuffle {}".format(shard, shuffle))

        np.random.shuffle(data_shard)
        file_name, num_batch_shuffle = create_ordered_tfrecords(
            save_dir, basename, np.concatenate(data_shard), bsz, tgt_len,
            num_core_per_host, cutoffs, bin_sizes, use_tpu=use_tpu)
        file_names.append(file_name)
        num_batch += num_batch_shuffle

    return file_names, num_batch


class Corpus(object):
    def __init__(self, path, dataset, *args, **kwargs):
        self.dataset = dataset
        self.vocab = Vocab(*args, **kwargs)

        self.vocab.count_file(os.path.join(path, "train.txt"))
        self.vocab.build_vocab()

        self.train = self.vocab.encode_file(
            os.path.join(path, "train.txt"), ordered=True)
        self.valid = self.vocab.encode_file(
            os.path.join(path, "valid.txt"), ordered=True)
        self.test = self.vocab.encode_file(
            os.path.join(path, "train.txt"), ordered=True)

        self.cutoffs = []

    def convert_to_tfrecords(self, split, save_dir, bsz, tgt_len,
                             num_core_per_host, **kwargs):
        FLAGS = kwargs.get('FLAGS')

        file_names = []
        use_tpu = FLAGS.use_tpu and not (split == "test" and num_core_per_host == 1)

        if use_tpu:
            record_name = "record_info-{}.bsz-{}.tlen-{}.core-{}.json".format(
                split, bsz, tgt_len, num_core_per_host)
        else:
            record_name = "record_info-{}.bsz-{}.tlen-{}.json".format(
                split, bsz, tgt_len)

        record_info_path = os.path.join(save_dir, record_name)

        if self.dataset in ["ptb", "wt2", "wt103", "enwik8", "tangshi", "doupo", "test", "zhihu", "poetry"]:
            data = getattr(self, split)

            bin_sizes = get_bin_sizes(
                data, bsz // num_core_per_host, tgt_len, self.cutoffs)
            file_name, num_batch = create_ordered_tfrecords(
                save_dir, split, data, bsz, tgt_len, num_core_per_host,
                self.cutoffs, bin_sizes,
                num_passes=FLAGS.num_passes if split == 'train' and use_tpu else 1,
                use_tpu=use_tpu)
            file_names.append(file_name)

        with open(record_info_path, "w") as fp:
            record_info = {
                "filenames": file_names,
                "bin_sizes": bin_sizes,
                "num_batch": num_batch
            }
            json.dump(record_info, fp)


def get_bin_sizes(data, batch_size, tgt_len, cutoffs, std_mult=[2.5, 2.5, 2.5]):
    """
      Note: the `batch_size` here should be per-core batch size
    """
    bin_sizes = []

    def _nearest_to_eight(x):  # so that it's faster on TPUs
        y = x - x % 8
        return y + 8 if x % 8 >= 4 else max(8, y)

    if cutoffs:
        num_batch = len(data) // batch_size // tgt_len

        data = data[:batch_size * num_batch * tgt_len]
        data = data.reshape(batch_size, num_batch, tgt_len)

        tot = batch_size * tgt_len
        for b, (left, right) in enumerate(zip(cutoffs[1:-1], cutoffs[2:])):
            mask = (data >= left) * (data < right)
            percents = mask.astype(np.float64).sum(2).sum(0) / tot
            mean = np.mean(percents)
            std = np.std(percents)

            bin_size = int(math.ceil(tgt_len * batch_size * (mean + std_mult[b] * std)))
            bin_size = _nearest_to_eight(bin_size)
            bin_sizes.append(bin_size)

    return bin_sizes


def _int64_feature(values):
    return tf.compat.v1.train.Feature(int64_list=tf.compat.v1.train.Int64List(value=values))


def _float_feature(values):
    return tf.compat.v1.train.Feature(float_list=tf.compat.v1.train.FloatList(value=values))


def batchify(data, batch_size, num_passes):
    """
      if use_tpu = True: num_passes > 1

      Since TPU training requires entire [bsz x tgt_len] chunks, it can discard
      as many as `bsz * tgt_len` tokens in training. When `bsz` and `tgt_len` are
      both large, as in the case of TPU training for Transformer-XL, the problem
      may lead to detectable performance drop.

      Here, we use multiple randomly shifted copies to deal with this problem.
    """
    if num_passes > 1:
        data_len = len(data)
        double_data = np.concatenate([data, data])
        data_list = []
        for i in range(num_passes):
            start = np.random.randint(0, data_len)
            data_list.append(double_data[start:start + data_len])
        data = np.concatenate(data_list)

    num_step = len(data) // batch_size
    data = data[:batch_size * num_step]
    data = data.reshape(batch_size, num_step)

    return data


def create_ordered_tfrecords(save_dir, basename, data, batch_size, tgt_len,
                             num_core_per_host, cutoffs=[], bin_sizes=[],
                             num_passes=1, use_tpu=False):
    # save_dir 就是tfrecord的路径
    if use_tpu:
        file_name = "{}.bsz-{}.tlen-{}.core-{}.tfrecords".format(
            basename, batch_size, tgt_len, num_core_per_host)
    else:
        file_name = "{}.bsz-{}.tlen-{}.tfrecords".format(
            basename, batch_size, tgt_len)

    save_path = os.path.join(save_dir, file_name)
    record_writer = tf.compat.v1.python_io.TFRecordWriter(save_path)

    batched_data = batchify(data, batch_size, num_passes)

    num_batch = 0
    for t in range(0, batched_data.shape[1] - 1, tgt_len):
        cur_tgt_len = min(batched_data.shape[1] - 1 - t, tgt_len)
        # drop the remainder if use tpu
        if use_tpu and cur_tgt_len < tgt_len:
            break
        if num_batch % 500 == 0:
            print("  processing batch {}".format(num_batch))
        for idx in range(batch_size):
            inputs = batched_data[idx, t:t + cur_tgt_len]
            labels = batched_data[idx, t + 1:t + cur_tgt_len + 1]

            # features dict
            feature = {
                "inputs": _int64_feature(inputs),
                "labels": _int64_feature(labels),
            }

            if len(cutoffs) > 0 and use_tpu:
                # validate `bin_sizes` and `cutoffs`
                assert len(cutoffs) - len(bin_sizes) == 2, \
                    "len(cutoffs) - len(bin_sizes) != 2"

                # mask for bin 0
                left, right = cutoffs[:2]
                inp_mask = ((inputs >= left) * (inputs < right)).astype(np.float32)
                tgt_mask = ((labels >= left) * (labels < right)).astype(np.float32)

                feature["inp_mask"] = _float_feature(inp_mask)
                feature["tgt_mask"] = _float_feature(tgt_mask)

                # refresh `inp_cnts` and `tgt_cnts` for each TPU core
                if idx % (batch_size // num_core_per_host) == 0:
                    inp_cnts = [0] * len(bin_sizes)
                    tgt_cnts = [0] * len(bin_sizes)

                head_labels = np.copy(labels)
                inp_pos_per_bin, tgt_pos_per_bin = [], []
                for b, (left, right) in enumerate(zip(cutoffs[1:-1], cutoffs[2:])):
                    inp_pos = np.where((inputs >= left) * (inputs < right))[0]
                    tgt_pos = np.where((labels >= left) * (labels < right))[0]
                    inp_pos_per_bin.append(inp_pos)
                    tgt_pos_per_bin.append(tgt_pos)

                    head_labels[tgt_pos] = cutoffs[1] + b

                feature["head_labels"] = _int64_feature(head_labels)

                # permutation feature
                def _add_perm_feature(feature, pos_per_bin, cnts, prefix):
                    for b, pos in enumerate(pos_per_bin):
                        idx_tuple = []
                        for p in pos:
                            if cnts[b] < bin_sizes[b]:
                                idx_tuple.append([p, cnts[b]])
                                cnts[b] += 1
                            else:
                                break

                        n_tup = len(idx_tuple)
                        tup = np.array(idx_tuple).reshape(n_tup * 2)

                        feature["{}_cnt_{}".format(prefix, b)] = _int64_feature([n_tup])
                        feature["{}_tup_{}".format(prefix, b)] = _int64_feature(tup)

                _add_perm_feature(feature, inp_pos_per_bin, inp_cnts, "inp")
                _add_perm_feature(feature, tgt_pos_per_bin, tgt_cnts, "tgt")

            example = tf.compat.v1.train.Example(features=tf.compat.v1.train.Features(feature=feature))
            record_writer.write(example.SerializeToString())

        num_batch += 1

    record_writer.close()
    print("Done writing {}. batches: {}".format(file_name, num_batch))

    return file_name, num_batch


def get_lm_corpus(data_dir, dataset):
    fn = os.path.join(data_dir, "cache.pkl")

    if exists(fn):
        print("Loading cached dataset...")
        with open(fn, "rb") as fp:
            corpus = pickle.load(fp)
    else:
        print("Producing dataset...")
        kwargs = {}
        if dataset in ["doupo", "test", "wt103", "zhihu", "poetry", "tangshi"]:
            kwargs["special"] = ["<eos>"]
            kwargs["lower_case"] = False

        corpus = Corpus(data_dir, dataset, **kwargs)

        print("Saving dataset...")
        with open(fn, "wb") as fp:
            pickle.dump(corpus, fp, protocol=2)

        corpus_info = {
            "vocab_size": len(corpus.vocab),
            "cutoffs": corpus.cutoffs,
            "dataset": corpus.dataset
        }
        with open(os.path.join(data_dir, "corpus-info.json"), "w") as fp:
            json.dump(corpus_info, fp)

    return corpus


def main(unused_argv):
    del unused_argv  # Unused

    corpus = get_lm_corpus(FLAGS.data_dir, FLAGS.dataset)  #

    save_dir = os.path.join(FLAGS.data_dir, "tfrecords")
    if not exists(save_dir):
        makedirs(save_dir)

    # test mode
    if FLAGS.per_host_test_bsz > 0:
        corpus.convert_to_tfrecords("test", save_dir, FLAGS.per_host_test_bsz,
                                    FLAGS.tgt_len, FLAGS.num_core_per_host,
                                    FLAGS=FLAGS)
        return

    for split, batch_size in zip(
            ["train", "valid"],
            [FLAGS.per_host_train_bsz, FLAGS.per_host_valid_bsz]):

        if batch_size <= 0: continue
        print("Converting {} set...".format(split))
        corpus.convert_to_tfrecords(split, save_dir, batch_size, FLAGS.tgt_len,
                                    FLAGS.num_core_per_host, FLAGS=FLAGS)


def load_record_info(record_info_dir, split, per_host_bsz, tgt_len,
                     num_core_per_host, use_tpu):
    if use_tpu:
        record_name = "record_info-{}.bsz-{}.tlen-{}.core-{}.json".format(
            split, per_host_bsz, tgt_len, num_core_per_host)
    else:
        record_name = "record_info-{}.bsz-{}.tlen-{}.json".format(
            split, per_host_bsz, tgt_len)

    record_info_path = os.path.join(record_info_dir, record_name)
    with open(record_info_path, "r") as fp:
        record_info = json.load(fp)

    return record_info


def get_input_fn(record_info_dir, split, per_host_bsz, tgt_len,
                 num_core_per_host, num_hosts=1, use_tpu=False):
    """Creates input function."""
    record_info = load_record_info(record_info_dir, split, per_host_bsz, tgt_len,
                                   num_core_per_host, use_tpu=use_tpu)

    # 读取一些batch size的信息 冗余
    file_names = record_info["filenames"]
    bin_sizes = record_info["bin_sizes"]
    num_batch = record_info["num_batch"]

    tf.compat.v1.logging.info("[{}] File names {}".format(split, file_names))

    def input_fn(params):
        # per-core batch size
        per_core_bsz = params["batch_size"]

        # data_dir could be a remote path, e.g., a google storage url
        data_dir = params["data_dir"]

        def parser(record):
            # preprocess "inp_perm" and "tgt_perm"
            def _process_perm_feature(example, prefix):
                for b in range(len(bin_sizes)):
                    cnt = example.pop("{}_cnt_{}".format(prefix, b))[0]
                    tup = example.pop("{}_tup_{}".format(prefix, b))

                    tup = tf.compat.v1.reshape(
                        tf.compat.v1.sparse_tensor_to_dense(tup),
                        shape=[cnt, 2])

                    # tf.compat.v1.float32
                    perm = tf.compat.v1.sparse_to_dense(
                        sparse_indices=tup,
                        output_shape=[tgt_len, bin_sizes[b]],
                        sparse_values=1.0,
                        default_value=0.0)

                    example["{}_perm_{}".format(prefix, b)] = perm

            # whether allow the last batch with a potentially shorter length
            if use_tpu:
                record_spec = {
                    "inputs": tf.compat.v1.FixedLenFeature([tgt_len], tf.compat.v1.int64),
                    "labels": tf.compat.v1.FixedLenFeature([tgt_len], tf.compat.v1.int64),
                }
            else:
                record_spec = {
                    "inputs": tf.compat.v1.VarLenFeature(tf.compat.v1.int64),
                    "labels": tf.compat.v1.VarLenFeature(tf.compat.v1.int64),
                }

            # permutation related features
            if bin_sizes and use_tpu:
                # tf.compat.v1.float32
                record_spec["inp_mask"] = tf.compat.v1.FixedLenFeature([tgt_len], tf.compat.v1.float32)
                record_spec["tgt_mask"] = tf.compat.v1.FixedLenFeature([tgt_len], tf.compat.v1.float32)

                record_spec["head_labels"] = tf.compat.v1.FixedLenFeature([tgt_len], tf.compat.v1.int64)

                for b in range(len(bin_sizes)):
                    record_spec["inp_cnt_{}".format(b)] = tf.compat.v1.FixedLenFeature([1], tf.compat.v1.int64)
                    record_spec["inp_tup_{}".format(b)] = tf.compat.v1.VarLenFeature(tf.compat.v1.int64)
                    record_spec["tgt_cnt_{}".format(b)] = tf.compat.v1.FixedLenFeature([1], tf.compat.v1.int64)
                    record_spec["tgt_tup_{}".format(b)] = tf.compat.v1.VarLenFeature(tf.compat.v1.int64)

            # retrieve serialized example
            example = tf.compat.v1.parse_single_example(
                serialized=record,
                features=record_spec)

            # transform permutation tuples to permutation matrices
            if bin_sizes and use_tpu:
                _process_perm_feature(example, "inp")
                _process_perm_feature(example, "tgt")

            # cast int64 into int32
            # cast sparse to dense
            for key in list(example.keys()):
                val = example[key]
                if tf.compat.v1.keras.backend.is_sparse(val):
                    val = tf.compat.v1.sparse.to_dense(val)
                if val.dtype == tf.compat.v1.int64:
                    val = tf.compat.v1.to_int32(val)
                example[key] = val

            if use_tpu:
                return example
            else:
                return example["inputs"], example["labels"]

        file_paths = []
        for file_name in file_names:
            file_path = os.path.join(data_dir, file_name)
            file_paths.append(file_path)

        if split == "train":
            dataset = tf.compat.v1.data.Dataset.from_tensor_slices(file_paths)
            if len(file_paths) > 1:
                dataset = dataset.shuffle(len(file_paths)).repeat()
                dataset = tf.compat.v1.data.TFRecordDataset(dataset)
            elif num_hosts > 1:
                host_id = params["context"].current_host
                # drop the remaining batches
                num_batch_per_host = num_batch // num_hosts

                my_start_sample_id = (host_id * num_batch_per_host * num_core_per_host *
                                      per_core_bsz)
                my_sample_num = num_batch_per_host * num_core_per_host * per_core_bsz
                dataset = tf.compat.v1.data.TFRecordDataset(dataset).skip(
                    my_start_sample_id).take(my_sample_num)
            else:
                dataset = tf.compat.v1.data.TFRecordDataset(dataset)

            dataset = dataset.map(parser).cache().repeat()
            dataset = dataset.batch(per_core_bsz, drop_remainder=True)
            dataset = dataset.prefetch(num_core_per_host * per_core_bsz)
        else:
            # do not shuffle, repeat or cache in evaluation
            dataset = tf.compat.v1.data.Dataset.from_tensor_slices(file_paths)
            dataset = tf.compat.v1.data.TFRecordDataset(dataset)
            dataset = dataset.map(parser)
            dataset = dataset.batch(per_core_bsz, drop_remainder=True)

        return dataset

    if split == "train" and num_hosts > 1:
        record_info["num_batch"] = num_batch // num_hosts

    return input_fn, record_info


def get_corpus_info(corpus_info_path):
    with open(corpus_info_path, "r") as fp:
        corpus_info = json.load(fp)
    return corpus_info


if __name__ == "__main__":
    FLAGS = flags.FLAGS
    flags.DEFINE_string("data_dir", None,
                        help="Location of the data corpus")
    flags.DEFINE_enum("dataset", "poetry",
                      ["ptb", "wt2", "wt103", "lm1b", "enwik8", "text8", "doupo", "test", "zhihu", "poetry","tangshi"],
                      help="Dataset name.")
    flags.DEFINE_integer("per_host_train_bsz", 60,
                         help="train batch size each host")
    flags.DEFINE_integer("per_host_valid_bsz", 60,
                         help="valid batch size each host")
    flags.DEFINE_integer("per_host_test_bsz", 0,
                         help="If > 0, enter test mode and process test set only."
                              "Otherwise, process train and dev sets only.")
    flags.DEFINE_integer("tgt_len", 70,
                         help="number of tokens to predict")
    flags.DEFINE_integer("max_batch", -1,
                         help="run in debug mode")
    flags.DEFINE_integer("num_core_per_host", 2,
                         help="8 for TPU v2.")
    flags.DEFINE_bool("debug", default=False,
                      help="Process only the first batch without shuffle for lm1b.")
    flags.DEFINE_integer("num_procs", 1,
                         help="number of processes")
    flags.DEFINE_integer("num_passes", 10,
                         help="number of passes when use_tpu=True")
    flags.DEFINE_integer("num_shuffle", 4,
                         help="number of shuffles for lm1b")
    flags.DEFINE_bool("use_tpu", True,
                      help="use tpu")

    tf.compat.v1.app.run(main)
