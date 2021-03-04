import math
from abc import abstractmethod
from datetime import datetime
import logging
import dill
import os

from torch.utils.tensorboard import SummaryWriter

import random
import numpy as np
from collections import OrderedDict

import torch
from torch import cuda
from torch.autograd import Variable

import data
import utils
from meters import AverageMeter
from discriminator import Discriminator, AttDiscriminator
from generator import LSTMModel, VarLSTMModel
# from train_generator import train_g
# from train_discriminator import train_d
from PGLoss import PGLoss


class ModelTrainer:
    def __init__(self, args):
        # Set model parameters
        args.encoder_embed_dim = 128
        args.encoder_layers = 2  # 4
        args.encoder_dropout_out = 0
        args.decoder_embed_dim = 128
        args.decoder_layers = 2  # 4
        args.decoder_out_embed_dim = 128
        args.decoder_dropout_out = 0
        args.bidirectional = False

        self.args = args

        self.set_gpu(args)
        self.load_dataset(args)
        self.create_meters()
        self.create_models(args)
        self.create_output_path(args)
        self.create_losses()
        self.handicap_discriminator()
        self.create_optimizers(args)
        self.summary_writer = SummaryWriter(self.checkpoints_path)

    def set_gpu(self, args):
        # args.gpuid = ""  # TODO disable cuda
        if args.gpuid[0] == -1:
            self.use_cuda = False
        else:
            torch.cuda.set_device(args.gpuid[0])
            self.use_cuda = True
        # self.use_cuda = (len(args.gpuid) >= 1)

        print("{0} GPU(s) are available".format(cuda.device_count()))
        print("Using GPU {}".format(args.gpuid[0]))

    def load_dataset(self, args):
        # Load dataset
        splits = ['train', 'valid']
        if data.has_binary_files(args.data, splits):
            dataset = data.load_dataset(
                args.data, splits, args.src_lang, args.trg_lang, args.fixed_max_len)
        else:
            dataset = data.load_raw_text_dataset(
                args.data, splits, args.src_lang, args.trg_lang, args.fixed_max_len)
        if args.src_lang is None or args.trg_lang is None:
            # record inferred languages in args, so that it's saved in checkpoints
            args.src_lang, args.trg_lang = dataset.src, dataset.dst

        print('| [{}] dictionary: {} types'.format(dataset.src, len(dataset.src_dict)))
        print('| [{}] dictionary: {} types'.format(dataset.dst, len(dataset.dst_dict)))

        for split in splits:
            print('| {} {} {} examples'.format(args.data, split, len(dataset.splits[split])))

        self.dataset = dataset

    def create_meters(self):
        g_logging_meters = OrderedDict()
        g_logging_meters['train_loss'] = AverageMeter()
        g_logging_meters['valid_loss'] = AverageMeter()
        g_logging_meters['train_acc'] = AverageMeter()
        g_logging_meters['valid_acc'] = AverageMeter()
        g_logging_meters['bsz'] = AverageMeter()  # sentences per batch

        d_logging_meters = OrderedDict()
        d_logging_meters['train_loss'] = AverageMeter()
        d_logging_meters['valid_loss'] = AverageMeter()
        d_logging_meters['train_acc'] = AverageMeter()
        d_logging_meters['valid_acc'] = AverageMeter()
        d_logging_meters['bsz'] = AverageMeter()  # sentences per batch

        self.g_logging_meters = g_logging_meters
        self.d_logging_meters = d_logging_meters

    @abstractmethod
    def create_generator(self, args):
        raise NotImplementedError()
        # self.generator = LSTMModel(args, self.dataset.src_dict, self.dataset.dst_dict, use_cuda=self.use_cuda)
        # self.generator = VarLSTMModel(args, self.dataset.src_dict, self.dataset.dst_dict, use_cuda=self.use_cuda)
        # print("Generator loaded successfully!")

    @abstractmethod
    def create_discriminator(self, args):
        raise NotImplementedError()
        # discriminator = Discriminator(args, dataset.src_dict, dataset.dst_dict, use_cuda=use_cuda)
        # self.discriminator = AttDiscriminator(args, self.dataset.src_dict, self.dataset.dst_dict, use_cuda=self.use_cuda)
        # print("Discriminator loaded successfully!")

    def create_models(self, args):
        self.create_generator(args)
        self.create_discriminator(args)

        if self.use_cuda:
            if torch.cuda.device_count() > 1:
                self.discriminator = torch.nn.DataParallel(self.discriminator).cuda()
                self.generator = torch.nn.DataParallel(self.generator).cuda()
            else:
                self.generator.cuda()
                self.discriminator.cuda()
        else:
            self.discriminator.cpu()
            self.generator.cpu()

    def create_output_path(self, args):
        # adversarial training checkpoints saving path
        path = os.path.join(args.model_file, str(datetime.now()))
        if not os.path.exists(path):
            os.makedirs(path)
        self.checkpoints_path = path

    def create_losses(self):
        # define loss function
        self.g_criterion = torch.nn.NLLLoss(ignore_index=self.dataset.dst_dict.pad(), reduction='sum')
        self.d_criterion = torch.nn.BCELoss()
        self.pg_criterion = PGLoss(ignore_index=self.dataset.dst_dict.pad(), size_average=True, reduce=True)

    def handicap_discriminator(self):
        # fix discriminator word embedding (as Wu et al. do)
        for p in self.discriminator.embed_src_tokens.parameters():
            p.requires_grad = False
        for p in self.discriminator.embed_trg_tokens.parameters():
            p.requires_grad = False

    def create_optimizers(self, args):
        # define optimizer
        self.g_optimizer = eval("torch.optim." + args.g_optimizer)(filter(lambda x: x.requires_grad,
                                                                     self.generator.parameters()),
                                                              args.g_learning_rate)

        self.d_optimizer = eval("torch.optim." + args.d_optimizer)(filter(lambda x: x.requires_grad,
                                                                     self.discriminator.parameters()),
                                                              args.d_learning_rate,
                                                              momentum=args.momentum,
                                                              nesterov=True)

    def write_summary(self, scores, batch_step):
        # main_name = os.path.basename(self.model_base_path)
        for var, val in scores.items():
            # self.summary_writer.add_scalar(f"{main_name}/{var}", val, batch_step)
            self.summary_writer.add_scalar(var, val, batch_step)
        # self.summary_writer.add_scalars(main_name, scores, batch_step)

    def pg_step(self, sample, batch_i, epoch, loader_len):
        print("Policy Gradient Training")

        sys_out_batch = self.generator(sample)  # 64 X 50 X 6632
        out_batch = sys_out_batch.contiguous().view(-1, sys_out_batch.size(-1))  # (64 * 50) X 6632

        _, prediction = out_batch.topk(1)
        prediction = prediction.squeeze(1)  # 64*50 = 3200
        prediction = torch.reshape(prediction, sample['net_input']['src_tokens'].shape)  # 64 X 50

        with torch.no_grad():
            reward = self.discriminator(sample['net_input']['src_tokens'], prediction)  # 64 X 1

        train_trg_batch = sample['target']  # 64 x 50

        pg_loss = self.pg_criterion(sys_out_batch, train_trg_batch, reward, self.use_cuda)
        sample_size = sample['target'].size(0) if self.args.sentence_avg else sample['ntokens']  # 64
        logging_loss = pg_loss / math.log(2)
        self.g_logging_meters['train_loss'].update(logging_loss.item(), sample_size)
        logging.debug(
            f"G policy gradient loss at batch {batch_i}: {pg_loss.item():.3f}, lr={self.g_optimizer.param_groups[0]['lr']}")
        self.g_optimizer.zero_grad()
        pg_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.generator.parameters(), self.args.clip_norm)
        self.g_optimizer.step()

        self.write_summary({"pg_train_loss": logging_loss}, batch_i + (epoch-1) * loader_len)

    def mle_generator_loss(self, sample):
        sys_out_batch = self.generator(sample)
        out_batch = sys_out_batch.contiguous().view(-1, sys_out_batch.size(-1))  # (64 X 50) X 6632
        trg_batch = sample['target'].view(-1)  # 64*50 = 3200

        loss = self.g_criterion(out_batch, trg_batch)
        return loss

    def mle_step(self, sample, batch_i, epoch, loader_len):
        # MLE training
        print("MLE Training")

        loss = self.mle_generator_loss(sample)
        sample_size = sample['target'].size(0) if self.args.sentence_avg else sample['ntokens']
        logging_loss = loss.data / sample_size / math.log(2)

        sample_size = sample['target'].size(0) if self.args.sentence_avg else sample['ntokens']
        nsentences = sample['target'].size(0)
        self.g_logging_meters['bsz'].update(nsentences)
        self.g_logging_meters['train_loss'].update(logging_loss, sample_size)
        logging.debug(
            f"G MLE loss at batch {batch_i}: {self.g_logging_meters['train_loss'].avg:.3f}, lr={self.g_optimizer.param_groups[0]['lr']}")
        self.g_optimizer.zero_grad()
        loss.backward()
        # all-reduce grads and rescale by grad_denom
        for p in self.generator.parameters():
            if p.requires_grad:
                p.grad.data.div_(sample_size)
        torch.nn.utils.clip_grad_norm_(self.generator.parameters(), self.args.clip_norm)
        self.g_optimizer.step()

        self.write_summary({"mle_train_loss": logging_loss}, batch_i + (epoch - 1) * loader_len)

    def discrimnator_loss_acc(self, sample):
        bsz = sample['target'].size(0)  # batch_size = 64
        src_sentence = sample['net_input']['src_tokens']  # 64 x max-len i.e 64 X 50

        # now train with machine translation output i.e generator output
        true_sentence = sample['target']  # 64*50 = 3200
        true_labels = Variable(torch.ones(sample['target'].size(0)).float())  # 64 length vector

        if self.use_cuda:
            true_sentence = true_sentence.cuda()
            true_labels = true_labels.cuda()

        with torch.no_grad():
            sys_out_batch = self.generator(sample)  # 64 X 50 X 6632

        out_batch = sys_out_batch.contiguous().view(-1, sys_out_batch.size(-1))  # (64 X 50) X 6632

        _, prediction = out_batch.topk(1)
        prediction = prediction.squeeze(1)  # 64 * 50 = 6632

        fake_labels = Variable(torch.zeros(sample['target'].size(0)).float())  # 64 length vector

        fake_sentence = torch.reshape(prediction, src_sentence.shape)  # 64 X 50

        if self.use_cuda:
            fake_labels = fake_labels.cuda()

        disc_out_neg = self.discriminator(src_sentence, fake_sentence)
        disc_out_pos = self.discriminator(src_sentence, true_sentence)
        disc_out = torch.cat([disc_out_neg.squeeze(1), disc_out_pos.squeeze(1)], dim=0)

        labels = torch.cat([fake_labels, true_labels], dim=0)

        d_loss = self.d_criterion(disc_out, labels)
        acc = torch.sum(torch.round(disc_out) == labels).float() / len(labels)
        return d_loss, acc

    def discriminator_step(self, sample, batch_i, epoch, loader_len):
        d_loss, acc = self.discrimnator_loss_acc(sample)

        self.d_logging_meters['train_acc'].update(acc)
        self.d_logging_meters['train_loss'].update(d_loss)
        logging.debug(
            f"D training loss {self.d_logging_meters['train_loss'].avg:.3f}, acc {self.d_logging_meters['train_acc'].avg:.3f} at batch {batch_i}")
        self.d_optimizer.zero_grad()
        d_loss.backward()
        self.d_optimizer.step()

        self.write_summary({"desc_train_loss": d_loss, "desc_train_acc": acc}, batch_i + (epoch - 1) * loader_len)

    def train_loop(self, trainloader, epoch_i, num_update):
        for i, sample in enumerate(trainloader):

            if self.use_cuda:
                # wrap input tensors in cuda tensors
                sample = utils.make_variable(sample, cuda=cuda)

            ## part I: use gradient policy method to train the generator

            # use policy gradient training when random.random() > 50%
            if random.random() >= 0.5:  # TODO why use both?
                self.pg_step(sample, i, epoch_i, len(trainloader))
            else:
                self.mle_step(sample, i, epoch_i, len(trainloader))
            num_update += 1

            # part II: train the discriminator
            self.discriminator_step(sample, i, epoch_i, len(trainloader))

        return num_update

    def train(self):
        args = self.args

        # start joint training
        best_dev_loss = math.inf
        num_update = 0
        # main training loop
        for epoch_i in range(1, args.epochs + 1):
            logging.info("At {0}-th epoch.".format(epoch_i))

            seed = args.seed + epoch_i
            torch.manual_seed(seed)

            max_positions_train = (args.fixed_max_len, args.fixed_max_len)

            # Initialize dataloader, starting at batch_offset
            trainloader = self.dataset.train_dataloader(
                'train',
                max_tokens=args.max_tokens,
                max_sentences=args.joint_batch_size,
                max_positions=max_positions_train,
                # seed=seed,
                epoch=epoch_i,
                sample_without_replacement=args.sample_without_replacement,
                sort_by_source_size=(epoch_i <= args.curriculum),
                shard_id=args.distributed_rank,
                num_shards=args.distributed_world_size,
            )

            # reset meters
            for key, val in self.g_logging_meters.items():
                if val is not None:
                    val.reset()
            for key, val in self.d_logging_meters.items():
                if val is not None:
                    val.reset()

            # set training mode
            self.generator.train()
            self.discriminator.train()
            update_learning_rate(num_update, 8e4, args.g_learning_rate, args.lr_shrink, self.g_optimizer)

            num_update = self.train_loop(trainloader, epoch_i, num_update)

            # validation
            # set validation mode
            self.generator.eval()
            self.discriminator.eval()
            # Initialize dataloader
            max_positions_valid = (args.fixed_max_len, args.fixed_max_len)
            valloader = self.dataset.eval_dataloader(
                'valid',
                max_tokens=args.max_tokens,
                max_sentences=args.joint_batch_size,
                max_positions=max_positions_valid,
                skip_invalid_size_inputs_valid_test=True,
                descending=True,  # largest batch first to warm the caching allocator
                shard_id=args.distributed_rank,
                num_shards=args.distributed_world_size,
            )

            # reset meters
            for key, val in self.g_logging_meters.items():
                if val is not None:
                    val.reset()
            for key, val in self.d_logging_meters.items():
                if val is not None:
                    val.reset()

            for i, sample in enumerate(valloader):

                with torch.no_grad():
                    if self.use_cuda:
                        # wrap input tensors in cuda tensors
                        sample = utils.make_variable(sample, cuda=cuda)

                    # generator validation
                    loss = self.mle_generator_loss(sample)
                    sample_size = sample['target'].size(0) if self.args.sentence_avg else sample['ntokens']
                    loss = loss.data / sample_size / math.log(2)

                    self.g_logging_meters['valid_loss'].update(loss, sample_size)
                    logging.debug(f"G dev loss at batch {i}: {self.g_logging_meters['valid_loss'].avg:.3f}")
                    self.write_summary({"mle_valid_loss": loss},
                                       i + (epoch_i - 1) * len(valloader))

                    # discriminator validation
                    d_loss, acc = self.discrimnator_loss_acc(sample)
                    self.d_logging_meters['valid_acc'].update(acc)
                    self.d_logging_meters['valid_loss'].update(d_loss)
                    logging.debug(f"D dev loss {self.d_logging_meters['valid_loss'].avg:.3f}, acc {self.d_logging_meters['valid_acc'].avg:.3f} at batch {i}")
                    self.write_summary({"desc_val_loss": d_loss, "desc_val_acc": acc},
                                       i + (epoch_i - 1) * len(valloader))

            self.save_models(epoch_i)

            if self.g_logging_meters['valid_loss'].avg < best_dev_loss:
                best_dev_loss = self.g_logging_meters['valid_loss'].avg
                self.save_generator(os.path.join(self.checkpoints_path, "best_gmodel.pt"))

    def save_generator(self, path):
        torch.save(self.generator, open(path, 'wb'), pickle_module=dill)

    def save_discriminator(self, path):
        torch.save(self.discriminator, open(path, 'wb'), pickle_module=dill)

    def save_models(self, epoch_i):
        self.save_generator(os.path.join(self.checkpoints_path, f"joint_{self.g_logging_meters['valid_loss'].avg:.3f}.epoch_{epoch_i}.pt"))
        self.save_generator(os.path.join(self.checkpoints_path, f"joint_{self.g_logging_meters['valid_loss'].avg:.3f}.epoch_{epoch_i}_discr.pt"))


def update_learning_rate(update_times, target_times, init_lr, lr_shrink, optimizer):

    lr = init_lr * (lr_shrink ** (update_times // target_times))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr