from ModelTrainer import ModelTrainer, update_learning_rate
import torch
from discriminator import Discriminator, AttDiscriminator, GumbelDiscriminator


class SeqT5Trainer(ModelTrainer):
    def __init__(self, args, task_prefix=""):
        """
        Init SeqT5 trainer
        :param args:
        :param task_prefix: For summarization use "summarize: ", for nmt use "translate English to German: "
        """
        super(SeqT5Trainer, self).__init__(args)
        self.task_prefix = torch.LongTensor(self.t5_tokenizer.encode(task_prefix))

    def create_generator(self, args):
        from transformers import T5Tokenizer
        from SeqT5 import SeqT5

        self.t5_tokenizer = T5Tokenizer.from_pretrained('t5-small')
        self.generator = SeqT5.from_pretrained('t5-small')

    def create_discriminator(self, args):
        # raise NotImplementedError()
        self.discriminator = AttDiscriminator(args, self.dataset.src_dict, self.dataset.dst_dict,
                                              use_cuda=self.use_cuda)
        print("Discriminator loaded successfully!")

    def create_models(self, args):
        self.create_generator(args)
        self.create_discriminator(args)

        if self.use_cuda:
            # if torch.cuda.device_count() > 1:
            #     self.discriminator = torch.nn.DataParallel(self.discriminator).cuda()
            #     self.generator = torch.nn.DataParallel(self.generator).cuda()
            # else:
            self.generator.cuda()
            if hasattr(self, "discriminator"):
                self.discriminator.cuda()
        else:
            if hasattr(self, "discriminator"):
                self.discriminator.cpu()
            self.generator.cpu()

    def handicap_discriminator(self):
        # TODO need this?
        # fix discriminator word embedding (as Wu et al. do)
        if hasattr(self, "discriminator"):
            for p in self.discriminator.embed_src_tokens.parameters():
                p.requires_grad = False
            for p in self.discriminator.embed_trg_tokens.parameters():
                p.requires_grad = False

    def transform_for_t5(self, tensor):
        return tensor - 1

    def transform_from_t5(self, tensor):
        return tensor + 1

    def wrap_for_output(self, sample, logits, input_onehot=None, output_onehot=None, target_onehot=None):
        if input_onehot is not None: # add zeros to use indexing from 1
            zeros = torch.zeros((input_onehot.shape[0], input_onehot.shape[1], 1)).to(input_onehot.device)
            input_onehot = torch.cat([zeros, input_onehot], dim=2)
            zeros = torch.zeros((output_onehot.shape[0], output_onehot.shape[1], 1)).to(output_onehot.device)
            output_onehot = torch.cat([zeros, output_onehot], dim=2)
            zeros = torch.zeros((target_onehot.shape[0], target_onehot.shape[1], 1)).to(target_onehot.device)
            target_onehot = torch.cat([zeros, target_onehot], dim=2)

        output = {
            "logits": logits,
            "target": sample["target"],
            "mask": self.get_length_mask(sample["target"]),
            "prediction": self.transform_from_t5(logits.argmax(-1)),
            "input_onehot": input_onehot,
            "output_onehot": output_onehot,
            "target_onehot": target_onehot,
        }

        output["loss"] = self.g_criterion(
            output["logits"][output["mask"], :],
            self.transform_for_t5(output["target"])[output["mask"]]
        )
        return output

    def sequential_generation(self, sample, decoding_style="rl", top_k=0, top_p=0.6, temp=.2):
        t5out = self.generator(
            self.transform_for_t5(sample['net_input']['src_tokens']),
            labels=self.transform_for_t5(sample['target']), decoding_style=decoding_style, top_k=top_k, top_p=top_p,
            temperature=temp, epsilon=self.args.imp_smpl_epsilon
        )

        if decoding_style == "gumbel":
            return self.wrap_for_output(sample, t5out.logits, input_onehot=t5out.input_onehot, output_onehot=t5out.output_onehot, target_onehot=t5out.target_onehot)
        return self.wrap_for_output(sample, t5out.logits)

    def teacher_forcing_generation(self, sample):
        logits = self.generator(
            self.transform_for_t5(sample['net_input']['src_tokens']),
            labels=self.transform_for_t5(sample['target']), decoding_style="tf"
        ).logits

        return self.wrap_for_output(sample, logits)

    def eval_generation(self, sample):
        return self.sequential_generation(sample, decoding_style=self.sequential_decoding_style, top_k=1, temp=.5)

    def save_generator(self, path):
        self.generator.save_pretrained(path)


class SeqT5Mle(SeqT5Trainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.training_strategy = "mle"  # alternate | mle | rl
        self.sequential_decoding_style = "rl"

    def create_discriminator(self, args):
        pass


class SeqT5RL(SeqT5Trainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.training_strategy = "alternate"  # alternate | mle | rl
        self.sequential_decoding_style = "rl"


class SeqT5Gumbel(SeqT5RL):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.training_strategy = "alternate"  # alternate | mle | rl
        self.sequential_decoding_style = "gumbel"

    def create_discriminator(self, args):
        self.discriminator = GumbelDiscriminator(args, self.dataset.src_dict, self.dataset.dst_dict,
                                              use_cuda=self.use_cuda)
        print("Discriminator loaded successfully!")
