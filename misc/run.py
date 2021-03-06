import sys
import os, shutil
import torch
import torch.nn as nn
sys.path.append("..")
sys.path.append(".")
from torch.nn.utils import clip_grad_value_
from tqdm import tqdm
from torch.utils.data import DataLoader
import numpy as np
import glob
from pandas.io.json import json_normalize
import json
import pandas as pd
from misc.cocoeval import suppress_stdout_stderr, COCOScorer, COCOBLEUScorer
import misc.utils as utils
from dataloader import VideoDataset


import math, random

from models.Translator import Translator, Translator_ensemble
import models.Constants as Constants

from models import get_model
from .optim import get_optimizer
from .crit import get_criterion
from .logger import CsvLogger, k_PriorityQueue

from collections import defaultdict
import pickle
import time

def save_checkpoint(state, is_best, filepath='./', filename='checkpoint.pth.tar', best_model_name='model_best.pth.tar'):
    if not os.path.exists(filepath):
        os.makedirs(filepath)
    save_path = os.path.join(filepath, filename) 
    torch.save(state, save_path)
    if is_best:
        best_path = os.path.join(filepath, best_model_name)
        shutil.copyfile(save_path, best_path)


def get_dict_mapping(opt, teacher_opt):
    if teacher_opt is None:
        return {}

    info = json.load(open(opt["info_json"]))
    vocab = info['ix_to_word']

    teacher_info = json.load(open(teacher_opt["info_json"]))
    teacher_vocab = teacher_info['ix_to_word']
    teacher_w2ix = teacher_info['word_to_ix']
    if vocab == teacher_vocab:
        return {}

    dict_mapping = {}
    for k, v in vocab.items():
        dict_mapping[int(k)] = int(teacher_w2ix[v])
    return dict_mapping

def enlarge(info, beam_size):
    bsz, *rest_shape = info.shape
    if len(rest_shape) == 2:
        return info.unsqueeze(1).repeat(1, beam_size, 1, 1).view(bsz * beam_size, *rest_shape)
    return info.unsqueeze(1).repeat(1, beam_size, 1).view(bsz * beam_size, *rest_shape)

def get_forword_results(opt, model, data, device, only_data=False, vocab=None):
    feats_i, feats_m, feats_a, feats_s, feats_t, category, labels, length_target, attribute = map(
            lambda x: x.to(device), 
            [data['feats_i'], data['feats_m'], data['feats_a'], data['feats_s'], data['feats_t'],
            data['category'], data['labels'], data['length_target'], data['attribute']]
        )

    feats_t = feats_t.mean(1)

    if opt['decoder_type'] in ['ARFormer', 'NARFormer']:
        if opt['na']:
            if opt['method'] == 'mp':
                tokens = data['tokens'].to(device)
            elif opt['method'] == 'nva':
                dec_source_1 = data['tokens_1'].to(device) # [batch_size, num_demand, max_len]
                tokens = [dec_source_1.view(-1, dec_source_1.shape[-1]), data['tokens'].to(device)]
            elif opt['method'] == 'direct' or opt['method'] == 'ap' or opt['method'] == 'signal' or opt['method'] == 'signal2':
                tokens = [data['tokens_1'].to(device), data['tokens'].to(device)]
            elif opt['method'] == 'signal3':
                tokens = [data['tokens_1'].to(device), data['tokens'].to(device), data['taggings'].to(device)]
            elif opt['method'] in ['nv', 'ms']:
                tokens = [data['tokens_1'].to(device), data['tokens'].to(device)]
        else:
            if opt['method'] == 'ag':
                tokens = [data['tokens_1'].to(device), data['tokens'].to(device)]
            else:
                tokens = data['tokens'].to(device)
    else:
        tokens = labels
        #pass
        #

    obj = None
    if data.get('obj', None) is not None:
        obj = data['obj'].to(device)
    #taggings = None
    #if opt.get('use_tag', False):
    taggings = data.get('taggings', None)
    if taggings is not None: 
        taggings = taggings.to(device)

    bert_embs = data.get('bert_embs', None)
    if bert_embs is not None:
        bert_embs = bert_embs.to(device)

    mapping = {
        'i': feats_i,
        'm': feats_m,
        'a': feats_a
    }
    
    modality = opt['modality'].lower()

    feats = []
    for char in modality:
        assert char in ['i', 'm', 'a']
        feats.append(mapping[char])

    if only_data:
        #print(labels)
        return model.encode(feats=feats, semantics=feats_s), category, labels, feats_t

    results = model(
        feats=feats,
        tgt_tokens=tokens, 
        category=category,
        opt=opt,
        obj=obj,
        taggings=taggings,
        semantics=feats_s,
        vocab=vocab,
        tags=feats_t,
        bert_embs=bert_embs
        )

    if opt['na']:
        if opt['method'] == 'mp':
            results[Constants.mapping['lang'][1]] = labels
        elif opt['method'] == 'nva':
            dec_target_1 = data['labels_1'].to(device) # [batch_size, num_demand, max_len]
            results[Constants.mapping['lang'][1]] = [dec_target_1.view(-1, dec_target_1.shape[-1]), labels]
        elif opt['method'] == 'ap' or opt['method'] == 'signal2':
            dec_target_1 = data['labels_1'].to(device) # [batch_size, num_demand, max_len]
            results[Constants.mapping['lang'][1]] = [dec_target_1, labels]
        elif opt['method'] == 'direct' or opt['method'] == 'signal':
            results[Constants.mapping['lang'][1]] = [data['labels_1'].to(device), labels, labels]
        elif opt['method'] == 'signal3':
            results[Constants.mapping['lang'][1]] = [
            data['labels_1'].to(device), 
            labels, 
            data['pure_target'].to(device)
            #enlarge(data['pure_target'].to(device), 2)
            ]
        elif opt['method'] == 'nv':
            results[Constants.mapping['lang'][1]] = [
            data['labels_1'].to(device), 
            labels
            ]
        elif opt['method'] == 'ms':
            results[Constants.mapping['lang'][1]] = [
            enlarge(data['labels_1'].to(device), opt['multiscale']), 
            labels
            ]
    else:
        if opt['method'] == 'ag':
            results[Constants.mapping['lang'][1]] = [
            data['labels_1'].to(device)[:, 1:], 
            labels[:, 1:]
            ]
        else:
            results[Constants.mapping['lang'][1]] = labels[:, 1:]

    results['obj'] = obj
    results['taggings'] = taggings[:, 1:] if taggings is not None else None
    results[Constants.mapping['length'][1]] = length_target
    results[Constants.mapping['bow'][1]] = data['pure_target'].to(device) if opt['method'] != 'nv' else data['labels_1'].to(device)
    results[Constants.mapping['attr'][1]] = attribute

    results['pure_target'] = data['pure_target'].to(device)

    

    #results['gold'] = data['pure_target'][:, 1:].to(device)
    '''
    tqdm.write('MASK Source: ' + to_sentence(tokens[0].tolist()[1:], vocab, break_words=[], skip_words=[Constants.PAD]))
    tqdm.write('MASK Target: ' + to_sentence(labels[0].tolist()[1:], vocab, break_words=[], skip_words=[Constants.PAD]))

    ind = labels[0, 1:].ne(Constants.PAD)
    pred = results['seq_probs'][0].max(-1)[1][0][ind]
    tqdm.write('MASK Predic: ' + to_sentence(pred.tolist(), vocab, break_words=[]))
    '''
    return results


def get_loader(opt, mode, print_info=False, specific=-1, target_ratio=-1):
    dataset = VideoDataset(opt, mode, print_info, specific=specific, target_ratio=target_ratio)
    if opt.get('splits_redefine_path', ''):
        dataset.set_splits_by_json_path(opt['splits_redefine_path']) 
    return DataLoader(
        dataset, 
        batch_size=opt["batch_size"], 
        shuffle=True if mode=='train' else False
        #shuffle=False
        )

def to_sentence(hyp, vocab, break_words=[Constants.EOS, Constants.PAD], skip_words=[]):
    sent = []
    for word_id in hyp:
        if word_id in skip_words:
            continue
        if word_id in break_words:
            break
        word = vocab[word_id]
        #if word == Constants.UNK_WORD:
        #    word = '-'
        sent.append(word)
    return ' '.join(sent)

def remove_repeat_n_grame(sent, n):
    length = len(sent)
    rec = {}
    result_sent = []
    for i in range(length-n+1):
        key = ' '.join(sent[i:i+n])
        if key in rec.keys():
            dis = i - rec[key] - n
            if dis in [0,1]:
                result_sent += sent[:i-dis]
                if i+n <length:
                    result_sent += sent[i+n:]
                return result_sent, False
        else:
            rec[key] = i
    return sent, True

def duplicate(sent):
    sent = sent.split(' ')
    res = {}
    for i in range(4, 0, -1):
        jud = False
        while not jud:
            sent, jud = remove_repeat_n_grame(sent, i)
            if not jud:
                res[i] = res.get(i, 0) + 1
            else:
                break
    res_str = []
    for i in range(1, 5):
        res_str.append('%d-gram: %d' % (i, res.get(i, 0)))
    return ' '.join(sent), '\t'.join(res_str)

def cal_score(opt, samples={'video9992': [{'image_id': 'video9992', 'caption': 'a man is riding a surfboard on a surfboard on the ocean'}],
    'video9997': [{'image_id': 'video9997', 'caption': 'a woman is applying her face'}]

    }, scorer=COCOScorer()):
    gt_dataframe = json_normalize(json.load(open(opt["input_json"]))['sentences'])
    gts = convert_data_to_coco_scorer_format(gt_dataframe)
    with suppress_stdout_stderr():
        valid_score = scorer.score(gts, samples, samples.keys())
    print(valid_score)

def cal_gt_n_gram(data, vocab, splits, n=1):
    gram_count = {}
    gt_sents = {}
    for i in splits['train']:
        k = 'video%d'% int(i)
        caps = data[k]
        for tmp in caps:
            cap = [vocab[wid] for wid in tmp[1:-1]]
            gt_sents[' '.join(cap)] = gt_sents.get(' '.join(cap), 0) + 1
            for j in range(len(cap)-n+1):
                key = ' '.join(cap[j:j+n])
                gram_count[key] = gram_count.get(key, 0) + 1
    return gram_count, gt_sents

def cal_n_gram(data, n=1):
    gram_count = {}
    sents = {}
    ave_length, count = 0, 0
    for k in data.keys():
        for i in range(len(data[k])):
            sents[data[k][i]['caption']] = sents.get(data[k][i]['caption'], 0) + 1
            cap = data[k][i]['caption'].split(' ')
            ave_length += len(cap)
            count += 1
            for j in range(len(cap)-n+1):
                key = ' '.join(cap[j:j+n])
                gram_count[key] = gram_count.get(key, 0) + 1
    return gram_count, sents, ave_length/count, count

def analyze_length_novel_unique(gt_data, data, vocab, splits, n=1, calculate_novel=True):
    novel_count = 0
    hy_res, hy_sents, ave_length, hy_count = cal_n_gram(data, n)
    if calculate_novel:
        gt_res, gt_sents = cal_gt_n_gram(gt_data, vocab, splits, n)
        for k1 in hy_sents.keys():
            if k1 not in gt_sents.keys():
                novel_count += 1

    

    novel = novel_count / hy_count
    unique = len(hy_sents.keys()) / hy_count
    vocabulary_usage = len(hy_res.keys())
    
    import spacy
    from collections import Counter
    nlp = spacy.load('en_core_web_sm')
    doc = ' '.join(list(hy_res.keys()))
    doc = nlp(doc)
    pos = [item.pos_ for item in doc]
    tmp = dict(Counter(pos))
    print(tmp)
    tmp_sum = 0
    for k, v in tmp.items():
        tmp_sum += v
    print("Noun/verb %d %.2f" % ((tmp['VERB'] + tmp['NOUN']), (tmp['VERB'] + tmp['NOUN']) / tmp_sum))

    gram4, _, _, _ = cal_n_gram(data, n=4)
    return ave_length, novel, unique, vocabulary_usage, hy_res, len(gram4)

def run_eval(opt, model, crit, loader, vocab, device, json_path='', json_name='', scorer=COCOScorer(), print_sent=False, teacher_model=None, length_crit=None, 
    no_score=False, save_videodatainfo=False,  saved_with_pickle=False, pickle_path=None, dict_mapping={}, analyze=False, collect_best_candidate_iterative_results=False, collect_path=None, 
    write_time=False, save_embs=False, save_to_spice=False, calculate_novel=True, evaluate_iterative_results=False, update_gram4=False):
    model.eval()
    if teacher_model is not None:
        teacher_model.eval()

    gts = loader.dataset.get_references()
    refs = defaultdict(list)
    spice_res = []
    samples = {}

    id_to_vid, now_mode = loader.dataset.get_mode()
    vatex = (opt['dataset'].lower() == 'vatex' and now_mode == 'test')
    vatex_samples = {}
    if vatex:
        assert id_to_vid is not None
        vid_to_id = {v: k for k, v in id_to_vid.items()}

    opt['collect_best_candidate_iterative_results'] = collect_best_candidate_iterative_results
    translator = Translator(model=model, opt=opt, teacher_model=teacher_model, dict_mapping=dict_mapping)

    best_candidate_sents = defaultdict(list)
    best_candidate_score = defaultdict(list)
    '''
    if opt.get('collect_last', False):
        best_candidate_sents = [[]]
        best_candidate_score = [[]]
    else:
        best_candidate_sents = [[] for _ in range(opt['iterations'] if opt.get("nv_scale", 0) != 100 else opt['iterations']+1)]
        best_candidate_score = [[] for _ in range(opt['iterations'] if opt.get("nv_scale", 0) != 100 else opt['iterations']+1)]
    '''
    best_ar_sent = []

    all_time = 0
    if save_embs:
        import h5py
        word_to_ix = {k:v for v, k in vocab.items()}
        embs_pth = './collect_embs'
        if not os.path.exists(embs_pth):
            os.makedirs(embs_pth)
        embs_db_name = os.path.basename(collect_path).split('.')[0] + '.hdf5'
        embs_db = h5py.File(os.path.join(embs_pth, embs_db_name), 'a')
        index_set = defaultdict(set)

    #target_sent = "a man is playing a video game"
    #target_count = 0
    #unique_sent = set()

    for data in tqdm(loader, ncols=150, leave=True):
        with torch.no_grad():
            encoder_outputs, category, labels, feats_t = get_forword_results(opt, model, data, device=device, only_data=True, vocab=vocab)
            if teacher_model is not None:
                teacher_encoder_outputs, _, _, _ = get_forword_results(opt, teacher_model, data, device=device, only_data=True, vocab=vocab)
            else:
                teacher_encoder_outputs = None

            if opt['batch_size'] == 1:
                start_time = time.time()
            all_hyp, all_scores = translator.translate_batch(encoder_outputs, category, labels, vocab, teacher_encoder_outputs=teacher_encoder_outputs, tags=feats_t)
            if opt['batch_size'] == 1:
                all_time += (time.time() - start_time)

            if isinstance(all_hyp, torch.Tensor):
                if len(all_hyp.shape) == 2:
                    all_hyp = all_hyp.unsqueeze(1)
                all_hyp = all_hyp.tolist()
            if isinstance(all_scores, torch.Tensor):
                if len(all_scores.shape) == 2:
                    all_scores = all_scores.unsqueeze(1)
                all_scores = all_scores.tolist()

            video_ids = np.array(data['video_ids']).reshape(-1)

        for k, hyps in enumerate(all_hyp):
            video_id = video_ids[k]
            samples[video_id] = []
            if not no_score: 
                assert len(hyps) == 1

            for j, hyp in enumerate(hyps):
                sent = to_sentence(hyp, vocab)
                if not opt.get('no_duplicate', False) and opt['decoder_type'] == 'NARFormer':
                    sent, _ = duplicate(sent)
                if print_sent:
                    tqdm.write(video_id + ': ' + sent)
                    #sent, res = duplicate(sent)
                    #tqdm.write(video_id + ': ' + res)
                    #tqdm.write(video_id + ': ' + sent)
                samples[video_id].append({'image_id': video_id, 'caption': sent})

                #if target_sent in sent:
                #    target_count += 1
                #    print(video_id)
                #unique_sent.add(sent)

                if vatex:
                    vatex_samples[vid_to_id[video_id]] = sent

                #if len(sent.split(' ')) <= 3:
                #    continue
                if save_to_spice:
                    tmp2 = []
                    for item in gts[video_id]:
                        tmp2.append(item['caption'])
                    tmp = {'image_id': video_id, 'test': sent, 'refs': tmp2}
                    spice_res.append(tmp)

                if save_videodatainfo:
                    refs[video_id].append({'image_id': video_id, 'cap_id': len(refs[video_id]), 'caption': sent, 'score': all_scores[k][j]})

        if collect_best_candidate_iterative_results:
            assert isinstance(all_scores, tuple)
            all_sents = all_scores[0].tolist()
            all_score = all_scores[1].tolist()

            if len(video_ids) != len(all_sents):
                video_ids = np.array(data['video_ids'])[:, np.newaxis].repeat(opt['length_beam_size'], axis=1).reshape(-1)
                assert len(video_ids) == len(all_sents)

            for k, (hyps, scores) in enumerate(zip(all_sents, all_score)):
                #video_id = video_ids[k]

                video_id = video_ids[k]
                pre_sent_len = 0
                assert len(hyps) == len(scores)  

                for j, (hyp, score) in enumerate(zip(hyps, scores)):
                    sent = to_sentence(hyp, vocab)
                    if save_embs:
                        utils.get_words_with_specified_tags(word_to_ix, sent, index_set[video_id])

                    if not pre_sent_len: 
                        pre_sent_len = len(sent.split(' '))
                    else:
                        assert len(sent.split(' ')) == pre_sent_len

                    tqdm.write(('%10s' % video_id) + '(iteration %d Length %d): ' % (j, len(sent.split(' '))) + sent)
                    '''
                    repetition_rate_result = calculate_repetition_rate(sent)
                    for n_gram in range(4):
                        repetition_rate_results[j][n_gram][0] += repetition_rate_result[n_gram][0]
                        repetition_rate_results[j][n_gram][1] += repetition_rate_result[n_gram][1]
                    '''
                    #best_candidate_sents[j].append([video_id, sent])
                    #best_candidate_score[j].append([video_id, score])
                    best_candidate_sents[video_id].append(sent)
                    best_candidate_score[video_id].append(score)

    #print(target_sent)
    #print(target_count)
    #print(list(unique_sent))

    if evaluate_iterative_results:
        assert collect_best_candidate_iterative_results
        keylist = list(best_candidate_sents.keys())
        itrs = len(best_candidate_sents[keylist[0]])
        b4 = []
        m = []
        r = []
        c = []
        for i in range(itrs):
            samples = {}
            for key in keylist:
                samples[key] = []
                samples[key].append({'image_id': key, 'caption': best_candidate_sents[key][i]})     
            
            with suppress_stdout_stderr():
                valid_score, detail_scores = scorer.score(gts, samples, samples.keys())
                b4.append(valid_score["Bleu_4"])
                m.append(valid_score["METEOR"])
                r.append(valid_score["ROUGE_L"])
                c.append(valid_score["CIDEr"])
        print(b4)
        print(m)
        print(r)
        print(c)
        #exit()
        no_score = True


    if save_to_spice:
        pth = './spice_json'
        if not os.path.exists(pth):
            os.makedirs(pth)
        if opt['na']:
            filename = '%s_%s_%s%s_lbs%d_i%d.json'%(opt['dataset'], opt['method'], 'AE' if opt['nv_scale'] else '', opt['paradigm'], opt['length_beam_size'], opt['iterations'])
        else:
            filename = '%s_%d.json' % (opt['dataset'], opt['beam_size'])
        json.dump(spice_res, open(os.path.join(pth, filename), 'w'))

    if collect_best_candidate_iterative_results:
        if save_embs:
            for k in index_set.keys():
                tmp = torch.LongTensor(list(index_set[k])).to(device)
                emb = model.decoder.bert.embedding.word_embeddings(tmp).mean(0).detach().cpu().numpy()
                print(emb.shape)
                embs_db[k] = emb

        assert collect_path is not None
        if not save_embs:
            pickle.dump(
                    [best_candidate_sents, best_candidate_score],
                    open(collect_path, 'wb')
                )

    if opt['batch_size'] == 1:
        latency = all_time/len(loader)

        if write_time:
            f = open('latency.txt', 'a')
            if opt['ar']:
                f.write('AR%d %s %.1f\n'%(opt['beam_size'], opt['dataset'], 1000*latency))
            else:
                f.write('NA %s %s %s %s %d%s%d %.1f\n'%(opt['method'], opt['dataset'], 'AE' if opt['nv_scale'] else '_', opt['paradigm'], opt['length_beam_size'], ' ' if opt['paradigm']=='mp' else (" %d "%opt['q']), opt['iterations'], 1000*latency))

        print(latency, len(loader))            

    res = {}
    if analyze:
        ave_length, novel, unique, usage, hy_res, gram4 = analyze_length_novel_unique(loader.dataset.captions, samples, vocab, splits=loader.dataset.splits, n=1, calculate_novel=calculate_novel)
        if update_gram4:
            res.update({'gram4': gram4})
        res.update({'ave_length': ave_length, 'novel': novel, 'unique': unique, 'usage': usage})   

    if not no_score:
        with suppress_stdout_stderr():
            valid_score, detail_scores = scorer.score(gts, samples, samples.keys())

        #json.dump(detail_scores, open('./nacf_ctmp_b6i5_135.json', 'w'))
        #json.dump(detail_scores, open('./nab_mp_b6i5_135.json', 'w'))
        #json.dump(detail_scores, open('./arb_b5.json', 'w'))
        #json.dump(detail_scores, open('./arb2_b5.json', 'w'))

        #print(detail_scores)
        res.update(valid_score)
        res['loss'] = 0
        if write_time:
            f.write("B4: %.2f\tM: %.2f\tR: %.2f\tC: %.2f\n" % (100 * res["Bleu_4"], 100 * res['METEOR'], 100 * res["ROUGE_L"], 100 * res["CIDEr"]))

        metric_sum = opt.get('metric_sum', [1, 1, 1, 1])
        res['Sum'] = 0
        candidate = [res["Bleu_4"], res["METEOR"], res["ROUGE_L"], res["CIDEr"]]
        for i, item in enumerate(metric_sum):
            if item: res['Sum'] += candidate[i]

        if json_path:
            if not os.path.exists(json_path):
                os.makedirs(json_path)
            #print('364 364 364 364')
            with open(os.path.join(json_path, json_name), 'w') as prediction_results:
                json.dump({"predictions": samples, "scores": valid_score}, prediction_results)
                prediction_results.close()

        def calculate_repetition_rate(sent, n):
            length = len(sent)
            rec = {}
            dd_count = 0
            for i in range(length-n+1):
                key = ' '.join(sent[i:i+n])
                if key in rec.keys():
                    dd_count += 1
                else:
                    rec[key] = i
            return dd_count, length-n+1

        dc, ac = 0, 0
        for key in samples.keys():
            tmp_dc, tmp_ac = calculate_repetition_rate(samples[key][0]['caption'].split(' '), n=1)
            dc += tmp_dc
            ac += tmp_ac
        #print('1-gram repetition rate: %.2f' % (100 * dc / ac))

        return res

    if json_path and vatex:
        if not os.path.exists(json_path):
            os.makedirs(json_path)
        with open(os.path.join(json_path, json_name), 'w') as prediction_results:
            json.dump(vatex_samples, prediction_results)

    if save_videodatainfo:
        if saved_with_pickle:
            assert pickle_path is not None
            pickle.dump(refs, open(pickle_path, 'wb'))
        else:
            model_name = '%s_%s' % (opt['encoder_type'], opt['decoder_type'])
            description = 'The sentences generated by %s where each video has %d captions.' % (model_name, opt['topk'])
            pth = os.path.join(opt['base_dir'], opt['dataset'], 'arvc%d_refs.pkl' % opt['topk'])

            pickle.dump(refs, open(pth, 'wb'))
            tqdm.write('Teacher videodatainfo has been saved to %s' % pth)
            cmd = 'python msvd_prepross.py -tdc %s -ori_wct %d -topk %d' % (opt['dataset'], opt['word_count_threshold'], opt['topk'])
            os.system(cmd)

    return res
    
def run_eval_ensemble(opt, opt_list, models, crit, loader, vocab, device, json_path='', json_name='', scorer=COCOScorer(), print_sent=False, no_score=False, analyze=False):
    translator = Translator_ensemble(model=models, opt=opt)

    videodatainfo = json.load(open(opt["input_json"]))
    gt_dataframe = json_normalize(videodatainfo['sentences'])
    gts = convert_data_to_coco_scorer_format(gt_dataframe) 
    samples = {}
    sentences = []
    
    for data in tqdm(loader, ncols=150, leave=False):
        with torch.no_grad():
            enc_output = []
            enc_hidden = []
            for i, model in enumerate(models):
                encoder_outputs, category, _ = get_forword_results(opt_list[i], model, data, device=device, only_data=True)
                enc_output.append(encoder_outputs['enc_output'])
                enc_hidden.append(encoder_outputs['enc_hidden'])

            all_hyp, all_scores = translator.translate_batch(enc_output, enc_hidden, category)

            if isinstance(all_hyp, torch.Tensor):
                if len(all_hyp.shape) == 2:
                    all_hyp = all_hyp.unsqueeze(1)
                all_hyp = all_hyp.tolist()
            video_ids = np.array(data['video_ids']).reshape(-1)

        for k, hyps in enumerate(all_hyp):
            video_id = video_ids[k]
            samples[video_id] = []
            if not no_score: 
                assert len(hyps) == 1
            index = 0
            for j, hyp in enumerate(hyps):
                sent = to_sentence(hyp, vocab)
                if not opt.get('no_duplicate', False) and opt['decoder_type'] == 'NARFormer':
                    sent, _ = duplicate(sent)
                if print_sent:
                    tqdm.write(video_id + ': ' + sent)
                samples[video_id].append({'image_id': video_id, 'caption': sent})
                if len(sent.split(' ')) <= 3:
                    continue
                sentences.append({'caption': sent, 'video_id': video_id, 'sen_id': index})
                index += 1
    res = {}
    if analyze:
        gt_caption = json.load(open(opt['caption_json']))
        ave_length, novel, unique, usage, hy_res = analyze_length_novel_unique(gt_caption, samples, n=1, dataset=opt['dataset'])
        res.update({'ave_length': ave_length, 'novel': novel, 'unique': unique, 'usage': usage})   

    if not no_score:
        with suppress_stdout_stderr():
            valid_score = scorer.score(gts, samples, samples.keys())

        res.update(valid_score)
        res['loss'] = 0

        metric_sum = opt.get('metric_sum', [1, 1, 1, 1])
        res['Sum'] = 0
        candidate = [res["Bleu_4"], res["METEOR"], res["ROUGE_L"], res["CIDEr"]]
        for i, item in enumerate(metric_sum):
            if item: res['Sum'] += candidate[i]

        if json_path:
            if not os.path.exists(json_path):
                os.makedirs(json_path)
            #print('364 364 364 364')
            with open(os.path.join(json_path, json_name), 'w') as prediction_results:
                json.dump({"predictions": samples, "scores": valid_score}, prediction_results)
                prediction_results.close()
        return res

    return None
        
def run_train(opt, model, crit, optimizer, loader, device, logger=None, length_crit=None, epoch=-1):
    model.train()
    crit.reset_loss_recorder()

    for data in tqdm(loader, ncols=150, leave=False):
        optimizer.zero_grad()
        results = get_forword_results(opt, model, data, device=device, only_data=False, vocab=loader.dataset.get_vocab())
        
        loss = crit.get_loss(results,epoch=epoch)
        loss.backward()

        clip_grad_value_(model.parameters(), opt['grad_clip'])
        optimizer.step()

    name, loss_info = crit.get_loss_info()
    if logger is not None:
        logger.write_text('\t'.join(['%10s: %05.3f' % (item[0], item[1]) for item in zip(name, loss_info)]))
    return loss_info[0]

def get_teacher_prob(epoch, ss_k = 100, linear = [200, 0.7], piecewise = [150, 0.95, 0.7], ss_type=1, max_epoch=500):
    if ss_type == 0:
        res = ss_k / (ss_k + math.exp(epoch/ss_k))
    elif ss_type == 1:
        if epoch >= max_epoch:
            return linear[1]
        slope = (linear[1] - 1) / (max_epoch - linear[0])
        b = linear[1] - max_epoch * slope
        res = slope * epoch + b
    else:
        a = (piecewise[1] - 1) / piecewise[0]**2
        slope = (piecewise[2] - piecewise[1]) / (max_epoch - piecewise[0])
        b = piecewise[2] - max_epoch * slope

        if epoch <= piecewise[0]:
            res = a * epoch * epoch + 1
        else:
            res = slope * epoch + b
    return res

def train_network_all(opt, model, device, first_evaluate_whole_folder=False):
    model.to(device)
    optimizer = get_optimizer(opt, model)
    crit = get_criterion(opt)

    folder_path = os.path.join(opt["checkpoint_path"], 'tmp_models')
    best_model = k_PriorityQueue(
        k_best_model = opt.get('k_best_model', 5), 
        folder_path = folder_path
        )

    #train_loader = get_loader(opt, 'train', print_info=False)
    vali_loader = get_loader(opt, 'validate', print_info=False)
    test_loader = get_loader(opt, 'test', print_info=False)
    vocab = vali_loader.dataset.get_vocab()
    logger = CsvLogger(
        filepath=opt["checkpoint_path"], 
        filename='trainning_record.csv', 
        fieldsnames=['epoch', 'train_loss', 'val_loss', 'Bleu_1', 'Bleu_2', 'Bleu_3', 'Bleu_4', 'METEOR', 'ROUGE_L', 'CIDEr', 'Sum']
        )

    if opt['decoder_type'] == 'NARFormer':
        opt['length_beam_size'] = 5
        opt['beam_alpha'] = 1.0
        opt['iterations'] = 5
        if opt['method'] == 'direct' or opt['method'] == 'signal' or opt['method'] == 'signal3' or opt['method'] == 'signal2':
            opt['iterations'] = 3

        if opt['dataset'] in ['VATEX', 'MSRVTT']:
            opt['iterations'] = 5
    else:
        opt['beam_size'] = 1
        opt['beam_alpha'] = 1.0

    if first_evaluate_whole_folder:
        assert os.path.exists(folder_path)
        best_model.load()
    else:
        start_epoch = 0
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)

        for epoch in tqdm(range(opt['epochs']), ncols=150, leave=False):
            if epoch < start_epoch: 
                continue 

            train_loader = get_loader(opt, 'train', print_info=False)
            #train_loader.dataset.shuffle()
            # decide the masking probability
            if opt['scheduled_sampling']:
                opt['teacher_prob'] = get_teacher_prob(epoch+1, opt['ss_k'], opt['ss_linear'], opt['ss_piecewise'], opt['ss_type'], max_epoch=opt.get('ss_epochs', opt['epochs']))
                train_loader.dataset.mask_prob = opt['teacher_prob']
            
            logger.write_text("epoch %d lr=%g (ss_prob=%g)" % (epoch, optimizer.get_lr(), opt.get('teacher_prob', 1)))
            # training
            

            train_loss = run_train(opt, model, crit, optimizer, train_loader, device, logger=logger, epoch=epoch)

            optimizer.epoch_update_learning_rate()
            # logging
            if epoch < opt['start_eval_epoch'] - 1:
                save_checkpoint(
                        {'epoch': epoch + 1, 'state_dict': model.state_dict(), 'validate_result': {}}, 
                        False, 
                        filepath=opt["checkpoint_path"], 
                        filename='checkpoint.pth.tar'
                    )
            elif (epoch+1) % opt["save_checkpoint_every"] == 0:
                res = run_eval(opt, model, crit, vali_loader, vocab, device)
                res['train_loss'] = train_loss
                res['epoch'] = epoch
                res['val_loss'] = res['loss']
                res.pop('loss')
                logger.write(res)

                save_checkpoint(
                        {'epoch': epoch + 1, 'state_dict': model.state_dict(), 'validate_result': res}, 
                        False, 
                        filepath=opt["checkpoint_path"], 
                        filename='checkpoint.pth.tar'
                    )
                
                model_name = 'model_%04d.pth.tar' % res['epoch']
                model_path = os.path.join(folder_path, model_name)
                not_break, info = best_model.check(res, model_path, model_name, opt)
                if not not_break:
                    # reach the tolerence
                    break
                logger.write_text(info)
                             
    results_path = os.path.join(opt["checkpoint_path"], 'best')
    if not os.path.exists(results_path):
        os.makedirs(results_path)

    if opt['dataset'].lower() == 'vatex':
        func = test_vatex
    else:
        func = test_na if opt['na'] else test_ar
    func(best_model, results_path, opt, model, crit, vali_loader, test_loader, vocab, device)
    shutil.rmtree(folder_path)


def test_ar(best_model, results_path, opt, model, crit, vali_loader, test_loader, vocab, device):
    length = min([best_model.qsize(), opt['k_best_model']])

    teacher_model_path = ""
    best_vali_res = 0
    all_epoch = []
    all_test_res = []
    all_model_path = []
    best_idx = -1
    
    for i in tqdm(range(length), ncols=150, leave=False):
        node = best_model.get()
        best_res = node.res
        best_model_path = node.model_path

        checkpoint = torch.load(best_model_path, 'cpu')
        #assert best_res == checkpoint['validate_result']

        model.load_state_dict(checkpoint['state_dict'])
        # bs5 test results
        opt['beam_size'] = 1
        opt['beam_alpha'] = 1.0

        vali_bs1_ba1 = run_eval(opt, model, crit, vali_loader, vocab, device)
        res = run_eval(opt, model, crit, test_loader, vocab, device, json_path=results_path, json_name='%d_bs1.json'%(best_res['epoch']), analyze=True)

        res['Vali_Sum'] = vali_bs1_ba1['Sum']
        res['epoch'] = best_res['epoch']
        res['beam_size'] = '1'
        res['beam_alpha'] = str(1)
        res['id'] = str(i)
        loop_logger = CsvLogger(filepath=results_path, filename='test_scores.csv', 
            fieldsnames=['id', 'Vali_Sum', 'epoch', 'Bleu_1', 'Bleu_2', 'Bleu_3', 'Bleu_4', 'METEOR', 'ROUGE_L', 'CIDEr', 'Sum', 'loss', 'beam_size', 'beam_alpha', 'novel', 'unique', 'usage', 'ave_length'])
        loop_logger.write(res)

        # bs1 validatation and test results
        opt['beam_size'] = 5
        test_res_record, test_sum_record = [], []
        vali_res_record, vali_sum_record = [], []
        tqdm.write('-------------------- %d --------------------' % i)
        for beam_alpha in [0, 0.5, 0.75, 1]:
            opt['beam_alpha'] = beam_alpha
            vali_result = run_eval(opt, model, crit, vali_loader, vocab, device)
            test_result = run_eval(opt, model, crit, test_loader, vocab, device, json_path=results_path, json_name='%d_bs5_ba%d.json'%(best_res['epoch'], int(100 * opt['beam_alpha'])), analyze=True)
            test_result['Vali_Sum'] = vali_result['Sum']
            test_result['epoch'] = best_res['epoch']
            test_result['beam_size'] = '5'
            test_result['beam_alpha'] = str(beam_alpha)
            test_result['id'] = str(i)
            loop_logger.write(test_result)

            tqdm.write('Vali\t%3.2f\t%5.2f\t%4.2f\t%4.2f\t%4.2f\t%4.2f' % (beam_alpha, vali_result['Sum'] * 100, vali_result['Bleu_4'] * 100, vali_result['METEOR'] * 100, vali_result['ROUGE_L'] * 100, vali_result['CIDEr'] * 100))
            tqdm.write('Test\t%3.2f\t%5.2f\t%4.2f\t%4.2f\t%4.2f\t%4.2f' % (beam_alpha, test_result['Sum'] * 100, test_result['Bleu_4'] * 100, test_result['METEOR'] * 100, test_result['ROUGE_L'] * 100, test_result['CIDEr'] * 100))
            vali_res_record.append(vali_result)
            vali_sum_record.append(vali_result['Sum'])
            test_res_record.append(test_result)
            test_sum_record.append(test_result['Sum'])

        test_sum_record.insert(0, res['Sum'])
        test_sum_record = ["%06d" % int(item * 1e5) for item in test_sum_record]
        test_sum_record.insert(0, "%04d" % best_res['epoch'])
        results_model_name = '_'.join(test_sum_record) +'.pth.tar'
        tqdm.write(results_model_name)
        #shutil.copy(best_model_path, os.path.join(results_path, results_model_name))
        save_checkpoint(
                {'epoch': checkpoint['epoch'], 
                'state_dict': checkpoint['state_dict'], 
                'validate_result': vali_res_record, 
                'test_result': test_res_record, 
                'settings': opt}, 
                False, 
                filepath=results_path, 
                filename=results_model_name
            )
        os.remove(best_model_path)

        all_epoch.append(best_res['epoch'])
        all_test_res.append(test_res_record.copy())
        all_model_path.append(os.path.join(results_path, results_model_name))

        vali_res = vali_sum_record[0] + vali_sum_record[-1]
        if vali_res > best_vali_res:
            best_vali_res = vali_res
            best_idx = i

    if max(all_epoch) - all_epoch[best_idx] >= 10:
        best_idx = all_epoch.index(max(all_epoch))

    save_path = os.path.join(opt["checkpoint_path"], 'teacher.pth.tar')
    tqdm.write("Teacher model: {} --- save to ---> {}".format(all_model_path[best_idx], save_path))
    shutil.copy(all_model_path[best_idx], save_path)

    for k in range(len(all_test_res[best_idx])):
        res = all_test_res[best_idx][k]
        tqdm.write('%5.2f\t%4.2f &%4.2f &%4.2f &%4.2f' % (res['Sum'] * 100, res['Bleu_4'] * 100, res['METEOR'] * 100, res['ROUGE_L'] * 100, res['CIDEr'] * 100))


def test_na(best_model, results_path, opt, model, crit, vali_loader, test_loader, vocab, device):
    length = min([best_model.qsize(), opt['k_best_model']])

    loop_logger = CsvLogger(filepath=results_path, filename='test_scores.csv', 
            fieldsnames=['Vali_Sum', 'id', 'epoch', 'Bleu_1', 'Bleu_2', 'Bleu_3', 'Bleu_4', 'METEOR', 'ROUGE_L', 'CIDEr', 'Sum', 'loss', 'iterations', 'lbs', 'beam_alpha', 'novel', 'unique', 'usage', 'ave_length'])

    for i in tqdm(range(length), ncols=150, leave=False):
        node = best_model.get()
        best_res = node.res
        best_model_path = node.model_path

        checkpoint = torch.load(best_model_path, 'cpu')
        #assert best_res == checkpoint['validate_result']

        results_model_name = ['%04d'%best_res['epoch'], '%06d'%int(best_res['Sum'] * 1e5)]
        model.load_state_dict(checkpoint['state_dict'])
        for iterations in [1, 2, 3, 5, 10]:
            opt['iterations'] = iterations
            res = run_eval(opt, model, crit, test_loader, vocab, device, 
                json_path=results_path, json_name='%d_lbs5_ba%d_i%d.json'%(best_res['epoch'], int(100 * opt['beam_alpha']), opt['iterations']), 
                teacher_model=None, dict_mapping=None, analyze=True)
            res['Vali_Sum'] = best_res['Sum']
            res['id'] = str(i)
            res['epoch'] = best_res['epoch']
            res['iterations'] = opt['iterations']
            res['lbs'] = opt['length_beam_size']
            res['beam_alpha'] = opt['beam_alpha']
            loop_logger.write(res)
            tqdm.write('Iterations: %2d\t%5.2f\t%4.2f\t%4.2f\t%4.2f\t%4.2f' % (iterations, res['Sum'] * 100, res['Bleu_4'] * 100, res['METEOR'] * 100, res['ROUGE_L'] * 100, res['CIDEr'] * 100))
            results_model_name.append('%06d'%int(res['Sum'] * 1e5))


        results_model_name = '_'.join(results_model_name) + '.pth.tar'
        tqdm.write(results_model_name)
        save_checkpoint({'epoch': checkpoint['epoch'], 'state_dict': checkpoint['state_dict'], 'validate_result': checkpoint['validate_result'],
                         'test_result': res, 'settings': opt}, 
                         False, filepath=results_path, filename=results_model_name)
        os.remove(best_model_path)

def test_vatex(best_model, results_path, opt, model, crit, vali_loader, test_loader, vocab, device):
    length = min([best_model.qsize(), opt['k_best_model']])

    loop_logger = CsvLogger(filepath=results_path, filename='test_scores.csv', 
                fieldsnames=['id', 'epoch', 'novel', 'unique', 'usage', 'ave_length'])

    for i in tqdm(range(length), ncols=150, leave=False):
        node = best_model.get()
        best_res = node.res
        best_model_path = node.model_path

        checkpoint = torch.load(best_model_path, 'cpu')
        best_res = checkpoint['validate_result']
        model.load_state_dict(checkpoint['state_dict'])

        if opt['na']:
            opt['iterations'] = 5
            opt['length_beam_size'] = 5
            opt['beam_alpha'] = 1.0
        else:
            opt['beam_size'] = 5
            opt['beam_alpha'] = 1.0
        res = run_eval(opt, model, crit, test_loader, vocab, device, json_path=results_path, json_name='%04d.json'%(best_res['epoch']), 
            analyze=True, no_score=True)

        res['epoch'] = best_res['epoch']
        res['id'] = str(i)
        loop_logger.write(res)

        results_model_name = ["%04d" % best_res['epoch'], '%06d' % int(1e5 * best_res['Sum']), '%06d' % int(1e5 * best_res['CIDEr'])]
        results_model_name = '_'.join(results_model_name) + '.pth.tar'
        tqdm.write(results_model_name)
        save_checkpoint(
                {'epoch': checkpoint['epoch'], 
                'state_dict': checkpoint['state_dict'], 
                'validate_result': best_res, 
                'test_result': res, 
                'settings': opt}, 
                False, 
                filepath=results_path, 
                filename=results_model_name
            )
        os.remove(best_model_path)
