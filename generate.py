import argparse
import json
import logging

import torch
import os
from torch import cuda
import options
import data
from generator import LSTMModel, VarLSTMModel

from sequence_generator import SequenceGenerator

logging.basicConfig(
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S', level=logging.DEBUG)

parser = argparse.ArgumentParser(
    description="Driver program for JHU Adversarial-NMT.")

# Load args
parser.add_argument("--model_name", default=None)
options.add_general_args(parser)
options.add_dataset_args(parser)
options.add_checkpoint_args(parser)
options.add_distributed_training_args(parser)
options.add_generation_args(parser)
options.add_generator_model_args(parser)


def main(args):

    model_name = args.model_name
    assert model_name is not None
    if model_name == "gan":
        Model = LSTMModel
    elif model_name == "vae":
        Model = VarLSTMModel
    elif model_name == "mle":
        Model = LSTMModel
    else:
        raise Exception("Model name should be: gan|vae|mle")

    if len(args.gpuid) >= 1 and args.gpuid[0] >= 0:
        use_cuda = True
        cuda.set_device(args.gpuid[0])
        map_to = torch.device(f"cuda:{args.gpuid[0]}")
    else:
        use_cuda = False
        map_to = torch.device('cpu')

    # Load dataset
    # if args.replace_unk is None:
    if data.has_binary_files(args.data, ['test']):
        dataset = data.load_dataset(
            args.data,
            ['test'],
            args.src_lang,
            args.trg_lang,
        )
    else:
        dataset = data.load_raw_text_dataset(
            args.data,
            ['test'],
            args.src_lang,
            args.trg_lang,
        )

    if args.src_lang is None or args.trg_lang is None:
        # record inferred languages in args, so that it's saved in checkpoints
        args.src_lang, args.trg_lang = dataset.src, dataset.dst

    print('| [{}] dictionary: {} types'.format(
        dataset.src, len(dataset.src_dict)))
    print('| [{}] dictionary: {} types'.format(
        dataset.dst, len(dataset.dst_dict)))
    print('| {} {} {} examples'.format(
        args.data, 'test', len(dataset.splits['test'])))

    # Set model parameters
    args.encoder_embed_dim = 128
    args.encoder_layers = 2  # 4
    args.encoder_dropout_out = 0
    args.decoder_embed_dim = 128
    args.decoder_layers = 2  # 4
    args.decoder_out_embed_dim = 128
    args.decoder_dropout_out = 0
    args.bidirectional = False

    # Load model
    if args.model_file is None:
        g_model_path = 'checkpoint/VAE_2021-03-04 12:16:21/best_gmodel.pt'
    else:
        g_model_path = args.model_file

    def load_params():
        params = json.loads(open(os.path.join(os.path.dirname(g_model_path), "params.json")).read())
        args.__dict__.update(params)

    load_params()

    assert os.path.exists(g_model_path), f"Path does not exist {g_model_path}"
    generator = Model(args, dataset.src_dict,
                          dataset.dst_dict, use_cuda=use_cuda)
    model_dict = generator.state_dict()
    model = torch.load(g_model_path, map_location=map_to)
    pretrained_dict = model.state_dict()
    # 1. filter out unnecessary keys
    pretrained_dict = {k: v for k,
                       v in pretrained_dict.items() if k in model_dict}
    # 2. overwrite entries in the existing state dict
    model_dict.update(pretrained_dict)
    # 3. load the new state dict
    generator.load_state_dict(model_dict)
    generator.eval()

    print("Generator loaded successfully!")

    if use_cuda > 0:
        generator.cuda()
    else:
        generator.cpu()

    max_positions = generator.encoder.max_positions()

    testloader = dataset.eval_dataloader(
        'test',
        max_sentences=args.max_sentences,
        max_positions=max_positions,
        skip_invalid_size_inputs_valid_test=args.skip_invalid_size_inputs_valid_test,
    )

    translator = SequenceGenerator(
        generator, beam_size=args.beam, stop_early=(not args.no_early_stop),
        normalize_scores=(not args.unnormalized), len_penalty=args.lenpen,
        unk_penalty=args.unkpen)

    if use_cuda:
        translator.cuda()

    with open('predictions.txt', 'w') as translation_writer:
        with open('real.txt', 'w') as ground_truth_writer:

            translations = translator.generate_batched_itr(
                testloader, maxlen_a=args.max_len_a, maxlen_b=args.max_len_b, cuda=use_cuda)

            for sample_id, src_tokens, target_tokens, hypos in translations:
                # Process input and ground truth
                target_tokens = target_tokens.int().cpu()
                src_str = dataset.src_dict.string(src_tokens, args.remove_bpe)
                target_str = dataset.dst_dict.string(
                    target_tokens, args.remove_bpe, escape_unk=True)

                # Process top predictions
                for i, hypo in enumerate(hypos[:min(len(hypos), args.nbest)]):
                    hypo_tokens = hypo['tokens'].int().cpu()
                    hypo_str = dataset.dst_dict.string(
                        hypo_tokens, args.remove_bpe)

                    hypo_str += '\n'
                    target_str += '\n'

                    # translation_writer.write(hypo_str.encode('utf-8'))
                    # ground_truth_writer.write(target_str.encode('utf-8'))
                    translation_writer.write(hypo_str)
                    ground_truth_writer.write(target_str)


if __name__ == "__main__":
    ret = parser.parse_known_args()
    args = ret[0]
    if ret[1]:
        logging.warning("unknown arguments: {0}".format(
            parser.parse_known_args()[1]))
    main(args)
