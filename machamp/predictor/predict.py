import logging
import os
import unicodedata 
from typing import List, Any, Dict

import torch
from torch.utils.data import DataLoader

logger = logging.getLogger(__name__)

from machamp.utils.lemma_edit import apply_lemma_rule
from machamp.utils.myutils import prep_batch, report_metrics
from machamp.data.machamp_dataset import MachampDataset
from machamp.data.machamp_sampler import MachampBatchSampler
from machamp.data.machamp_vocabulary import MachampVocabulary


def top_n_to_label(labels: List[Any], probs: List[float]):
    """
    Helper function to convert a list of labels and probabilities to a string.
    Goes from ['NOUN', 'VERB'] and [0.5, 0.3] to 'NOUN=0.5|VERB=0.3'
    
    Parameters
    ----------
    labels: List[Any]
        A list of labels, are commonly string, but could also be ints for example
        for dependency parsing.
    probs: List[float]
        A list of probabilities, which should have the same length as labels.

    Returns
    -------
    string_representation: str
        A string representation of the two lists, separating each item with a |, and 
        each label-probability pair with a =.
    """
    return '|'.join([label + '=' + str(prob) for label, prob in zip(labels, probs)])


def to_string(full_data: List[Any],
              preds: Dict[str, Dict[str, Any]],
              config: Dict,
              no_unk_subwords: List[str] = None,
              vocabulary: MachampVocabulary = None,
              token_ids: torch.tensor = None,
              ):
    """
    Combines the original input (fullData), and the predictions (preds) to a string format, 
    so that it can be saved to disk.
    
    Parameters
    ----------
    full_data: List[Any]
        A list of lists of strings or a list of strings (depends on task-type).
    preds: Dict[str,Dict[str, Any]] 
        The predictions from the model, the name of the task is the key, and the values are also 
        dictionaries. These dictionaries contain either the word-level labels or the sentence-level 
        labels. They can also include top-n labels, and in that case there should be a probs key as 
        well that contains the output of the softmax over the logits.
    config: Dict
        The dataset configuration, can be used to find task_types.
    no_unk_subwords: List[str]
        The string representation of the subwords. If a subword == unk, this actually
        kept the original string, so it is not always correctly obtainable from the
        token_ids, hence we need it separately.
    vocabulary: MachampVocabulary
        Stores the vocabularies for the output spaces of all tasks
    token_ids: torch.tensor
        Contains the token_ids for this instance, only useful if a "tok" task is included, otherwise
        the original tokens read from the input file are kept.

    Returns
    -------
    string_representation: str
        A single string (that can contain multiple lines), in which the full original input is there, 
        except that the label indices for the tasks we tackle is replaced by the predictions.
    """

    # This code can be much shorter if the if probs in preds[task] can be propagated to the function above

    # For word level annotation tasks, we have a different handling
    # so first detect whether we only have sentence level tasks
    task_types = [config['tasks'][task]['task_type'] for task in config['tasks']]
    only_sent = sum([task_type in ['classification', 'regression'] for task_type in task_types]) == len(config['tasks'])
    # from transformers import AutoTokenizer
    # tokzr = AutoTokenizer.from_pretrained('bert-base-multilingual-cased')
    if only_sent:
        for task in config['tasks']:
            task_idx = config['tasks'][task]['column_idx']
            if 'probs' in preds[task]:
                full_data[task_idx] = top_n_to_label(preds[task]['sent_labels'], preds[task]['probs'])
            else:
                full_data[task_idx] = preds[task]['sent_labels']
        return '\t'.join(full_data)
    else:  # word level annotation
        has_tok = 'tok' in task_types
        if has_tok:

            tok_task = None
            for task in config['tasks']:
                if config['tasks'][task]['task_type'] == 'tok':
                    tok_task = task
                    break
            tok_pred = preds[tok_task]

            # only keep the comments
            new_full_data = []
            for line in full_data:
                if line[0].startswith('#'):
                    new_full_data.append(line)
            full_data = new_full_data
            num_comments = len(full_data)

            # The first token has nothing to merge or split to, so it
            # has special handling (this just ensures we get into the "else"
            shifted_tok_pred = ['split'] + tok_pred['word_labels']
            for subword_idx in range(len(no_unk_subwords)):

                if shifted_tok_pred[subword_idx] == 'merge' and subword_idx > 0:
                    full_data[-1][1] += no_unk_subwords[subword_idx]
                else:
                    full_data.append([''] * 10)  # TODO 10 is hardcoded, 1 as well
                    full_data[-1][1] += no_unk_subwords[subword_idx]

            # We have to do this here, because diacritics were split from characters
            # they can only be merged back now that they are not separate subwords anymore
            for i in range(num_comments, len(full_data)):
                full_data[i][1] = unicodedata.normalize('NFC', full_data[i][1])

                
            # TODO hardcoded word indexes location for now (column 0)
            for i in range(num_comments, len(full_data)):
                full_data[i][0] = str(i - num_comments + 1)

        for task in config['tasks']:
            task_type = config['tasks'][task]['task_type']
            if task_type == 'tok':
                continue
            for num_comments in range(len(full_data)):
                if len(full_data[num_comments]) == len(full_data[-1]):
                    break

            if task_type in ['classification', 'regression']:
                found = False
                for comment_idx in range(num_comments):
                    if full_data[comment_idx][0].startswith('# ' + task + ': '):
                        if 'probs' in preds[task]:
                            full_data[comment_idx][0] = '# ' + task + ': ' + top_n_to_label(preds[task]['sent_labels'],
                                                                                            preds[task]['probs'])
                        else:
                            full_data[comment_idx][0] = '# ' + task + ': ' + preds[task]['sent_labels']

            else:
                task_idx = config['tasks'][task]['column_idx']
                for token_idx in range(len(full_data) - num_comments):
                    # Handle dependency parsing separately, because it uses 2 columns
                    if task_type == 'dependency':
                        if 'indice_probs' in preds[task]:
                            full_data[token_idx + num_comments][task_idx] = top_n_to_label(
                                preds[task]['dep_indices'][token_idx], preds[task]['indice_probs'][token_idx])
                            full_data[token_idx + num_comments][task_idx + 1] = top_n_to_label(
                                preds[task]['dep_labels'][token_idx], preds[task]['tag_probs'][token_idx])
                        else:
                            full_data[token_idx + num_comments][task_idx] = str(preds[task]['dep_indices'][token_idx])
                            full_data[token_idx + num_comments][task_idx + 1] = preds[task]['dep_labels'][token_idx]
                        continue

                    # For string2string, we convert the data first
                    if task_type == 'string2string':
                        token = full_data[token_idx + num_comments][config['word_idx']]
                        if 'probs' in preds[task]:
                            preds[task]['word_labels'][token_idx] = [apply_lemma_rule(token, pred) for pred in
                                                                     preds[task]['word_labels'][token_idx]]
                        else:
                            preds[task]['word_labels'][token_idx] = apply_lemma_rule(token, preds[task]['word_labels'][
                                token_idx])

                    if 'probs' in preds[task]:
                        full_data[token_idx + num_comments][task_idx] = top_n_to_label(
                            preds[task]['word_labels'][token_idx], preds[task]['probs'][token_idx])
                    else:
                        full_data[token_idx + num_comments][task_idx] = preds[task]['word_labels'][token_idx]

        return '\n'.join(['\t'.join(token_info) for token_info in full_data]) + '\n'


# this gets a dataloader and output folder, then it writes for multiple datasets at once
def predict(model, dev_dataloader, serialization_dir, dataset_configs, sep_token_id, batch_size, device, vocabulary):
    model.eval()
    model.reset_metrics()
    out_file = None
    prev_dataset = ''
    for batchIdx, batch in enumerate(dev_dataloader):
        dataset = batch[0].dataset
        if dataset != prev_dataset:
            if out_file != None:
                out_file.close()
            out_file = open(os.path.join(serialization_dir, dataset + '.out'), 'w')
            prev_dataset = dataset

        enc_batch = prep_batch(batch, device, dev_dataloader.dataset)
        out_dict = model.get_output_labels(enc_batch['token_ids'], enc_batch['golds'], enc_batch['seg_ids'],
                                           enc_batch['eval_mask'], enc_batch['offsets'], enc_batch['subword_mask'])
        for i in range(len(batch)):
            sent_dict = {}
            for task in out_dict:
                sent_dict[task] = {}
                for key in out_dict[task]:
                    sent_dict[task][key] = out_dict[task][key][i]
            output = to_string(batch[i].full_data, sent_dict, dataset_configs[dataset], batch[i].no_unk_subwords,
                               model.vocabulary, enc_batch['token_ids'][i], )
            out_file.write(output + '\n')
    metrics = model.get_metrics()
    report_metrics(metrics)


# TODO (a portion of) these 2 predict functions should be merged
# also quite some arguments seem to be unused?

# This gets paths as input
def predict2(model, input_path, output_path, dataset, batch_size, raw_text, device):
    model.eval()
    model.reset_metrics()
    if dataset == None:
        if len(model.dataset_configs) > 1:
            logger.error(
                'Error, please indicate the dataset with --dataset, so that MaChAmp knows how to read the data')
            exit(1)
        dataset = list(model.dataset_configs.keys())[0]
    data_config = {dataset: model.dataset_configs[dataset]}
    data_config[dataset]['dev_data_path'] = input_path
    dev_dataset = MachampDataset(model.mlm.name_or_path, data_config, is_train=False, vocabulary=model.vocabulary)
    dev_sampler = MachampBatchSampler(dev_dataset, batch_size, 1024, False, 1.0, False) # 1024 hardcoded
    dev_dataloader = DataLoader(dev_dataset, batch_sampler=dev_sampler, collate_fn=lambda x: x)

    outfile = open(output_path, 'w')
    for batch in dev_dataloader:
        enc_batch = prep_batch(batch, device, dev_dataset)
        out_dict = model.get_output_labels(enc_batch['token_ids'], enc_batch['golds'], enc_batch['seg_ids'],
                                           enc_batch['eval_mask'], enc_batch['offsets'], enc_batch['subword_mask'])
        for i in range(len(batch)):
            sent_dict = {}
            for task in out_dict:
                sent_dict[task] = {}
                for key in out_dict[task]:
                    sent_dict[task][key] = out_dict[task][key][i]
            output = to_string(batch[i].full_data, sent_dict, data_config[dataset], batch[i].no_unk_subwords,
                               model.vocabulary, enc_batch['token_ids'][i], )
            outfile.write(output + '\n')
    metrics = model.get_metrics()
    report_metrics(metrics)
