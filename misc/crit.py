from .logger import AverageMeter
import torch
import torch.nn as nn
import models.Constants as Constants
from torch.autograd import Variable
class BagOfWordsLoss(nn.Module):
    def __init__(self, ignore_index=Constants.PAD):
        super(BagOfWordsLoss, self).__init__()
        self.loss_fn = nn.BCEWithLogitsLoss(reduce=False)
        self.ignore_index = ignore_index

    def forward(self, scores, target):
        """
        scores: shape of [batch_size, max_len - 1, vocab_size]
        target: shape of [batch_size, max_len - 1]
        """
        assert target.size(1) == scores.size(1)
        shape = scores.shape
        device = scores.device

        mask = target.ne(self.ignore_index).float()
        mask_scores = mask.unsqueeze(-1) * scores
        sum_scores = mask_scores.sum(1)

        #print(sum_scores.shape)
        #print(target.shape)
        labels = Variable(torch.zeros(shape)).to(device)
        labels = labels.scatter_(2, target.unsqueeze(-1), 1).sum(1) # [batch_size, vocab_size]
        labels[:, self.ignore_index] = 0 # pad
        labels[:, Constants.MASK] = 0
        labels = labels.gt(0).float()

        loss = self.loss_fn(sum_scores, labels)

        return torch.sum(loss) / shape[0]

class RewardCriterion(nn.Module):

    def __init__(self):
        super(RewardCriterion, self).__init__()

    def forward(self, input, seq, reward):
        input = input.contiguous().view(-1)
        reward = reward.contiguous().view(-1)
        mask = (seq > 0).float()
        mask = torch.cat([mask.new(mask.size(0), 1).fill_(1).cuda(),
                         mask[:, :-1]], 1).contiguous().view(-1)
        output = - input * reward * mask
        output = torch.sum(output) / torch.sum(mask)

        return output

'''
class CrossEntropyLoss(nn.Module):
    def __init__(self, ignore_index=-100):
        super(CrossEntropyLoss, self).__init__()
        self.loss_fn = nn.NLLLoss(ignore_index=ignore_index)

    def forward(self, logits, target):
        """
        logits: shape of [batch_size, max_len - 1, vocab_size], logsoftmax
        target: shape of [batch_size, max_len - 1]
        """
        assert target.size(1) == logits.size(1)
        batch_size = logits.shape[0]
        logits = logits.contiguous().view(-1, logits.shape[2])
        target = target.contiguous().view(-1)


        loss = self.loss_fn(logits, target)
        return loss
'''

class CrossEntropyLoss(nn.Module):
    def __init__(self, ignore_index=None):
        super(CrossEntropyLoss, self).__init__()
        self.loss_fn = nn.NLLLoss(reduce=False)
        self.ignore_index = ignore_index

    def forward(self, logits, target):
        """
        logits: shape of [batch_size, max_len - 1, vocab_size]
        target: shape of [batch_size, max_len - 1]
        """
        assert target.size(1) == logits.size(1)
        batch_size = logits.shape[0]
        logits = logits.contiguous().view(-1, logits.shape[2])
        target = target.contiguous().view(-1)

        if self.ignore_index is not None:
            mask = target.ne(self.ignore_index).float()

        loss = self.loss_fn(logits, target)

        if self.ignore_index is not None:
            return torch.sum(loss * mask) / batch_size
        return torch.sum(loss) / batch_size

class DistillationLoss(nn.Module):
    def __init__(self, ignore_index):
        super(DistillationLoss, self).__init__()
        self.loss_fn = nn.MSELoss(reduce=False)
        self.ignore_index = ignore_index

    def forward(self, pred_embs, bert_embs, target):
        """
        logits: shape of [batch_size, max_len - 1, vocab_size]
        target: shape of [batch_size, max_len - 1]
        """
        bsz, seq_len, s = pred_embs.shape
        bert_embs = bert_embs[:, :seq_len, :]
        #print(pred_embs.shape, bert_embs.shape)
        bert_embs = bert_embs.contiguous().view(-1, s)
        pred_embs = pred_embs.contiguous().view(-1, s)
        target = target.contiguous().view(-1)
        #print(bert_embs.shape, pred_embs.shape, target.shape)
        loss = self.loss_fn(pred_embs, bert_embs).sum(-1) 
        #print(loss.shape)
        mask = target.ne(self.ignore_index).float()

        return torch.sum(loss * mask) / (mask.sum() * s)

class AttributeLoss(nn.Module):
    def __init__(self):
        super(AttributeLoss, self).__init__()
        self.loss_fn = nn.BCELoss(reduce=False)

    def forward(self, pred, target):
        """
        logits: shape of [batch_size, max_len - 1, vocab_size]
        target: shape of [batch_size, max_len - 1]
        """

        loss = self.loss_fn(pred, target).sum(-1) / (target.gt(0).sum(-1).float() + 1e-6)
        #print(loss.shape)
        return loss.sum() / target.size(0)

class Criterion(object):
    def __init__(self, 
        crit=[CrossEntropyLoss(ignore_index=Constants.PAD)], 
        keys=[('seq_probs', 'gold')], 
        names=['CAP_LOSS'], 
        scales=[1.0],
        calculate_word_acc=0,
        opt={}
        ):
        assert len(crit) == len(keys)
        assert len(keys) == len(names)
        assert len(names) == len(scales)
        self.num_loss = len(crit)
        self.crit = crit
        self.keys = keys
        self.names = names
        self.scales = scales
        self.calculate_word_acc = calculate_word_acc
        self.opt = opt

        self.bow_index = -1
        for i, item in enumerate(self.crit):
            if isinstance(item, BagOfWordsLoss):
                self.bow_index = i
                print('BOW index %d' % i)
                break

        self.weights = opt.get('nv_weights', [0.8, 1.0])
        
    def reset_loss_recorder(self):
        # before training a epoch
        self.loss_recorder = [AverageMeter() for _ in range(self.num_loss)]
        self.word_acc_recorder = [AverageMeter() for _ in range(self.calculate_word_acc)]

    def check_and_cal(self, pred, gt, crit):
        if isinstance(pred, list):
            i_loss = []
            num_sample = 0
            if isinstance(gt, list):
                assert len(pred) == len(gt)
                if self.opt['method'] in ['nv', 'ag']:
                    assert len(self.weights) == len(gt)
                index = 0
                for p, g in zip(pred, gt):
                    item = crit(p, g)
                    if self.opt['method'] in ['nv', 'ag']:
                        i_loss.append(item * self.weights[index])
                        index += 1
                    else:
                        i_loss.append(item)
                    num_sample += g.size(0)
            else:
                for i in range(len(pred)):
                    i_loss.append(crit(pred[i], gt))
                    num_sample += gt.size(0)
            i_loss = torch.stack(i_loss, dim=0).sum(0)
        else:
            i_loss = crit(pred, gt)
            num_sample = gt.size(0)

        return num_sample, i_loss

    def check_and_cal_word_acc(self, pred, gt):
        assert isinstance(pred, list)
        
        if isinstance(gt, list):
            assert len(pred) == len(gt)
            for i in range(len(pred)):
                ind = gt[i].ne(Constants.PAD)
                if i == 0:
                    if self.opt['method'] == 'signal3':
                        ind = ind & gt[i].ne(self.opt['visual_tag'])
                        #ind = ind & gt[i].ne(Constants.MASK)
                    elif self.opt['method'] == 'ag':
                        ind = ind & gt[i].ne(Constants.BOS)
                    else:
                        ind = ind & gt[i].ne(Constants.MASK)
                elif i == 1 and self.opt['method'] == 'signal3':
                    ind = ind & gt[i].ne(self.opt['nonvisual_tag'])
                    #ind = ind & gt[i].ne(Constants.MASK)
                
                predict_res = pred[i].max(-1)[1][ind]
                target_res = gt[i][ind]

                self.word_acc_recorder[i].update(
                            (predict_res==target_res).sum().item(), 
                            predict_res.size(0), 
                            multiply=False
                    )
        else:
            for i in range(len(pred)):
                ind = gt.ne(Constants.PAD)
                predict_res = pred[i].max(-1)[1][ind]
                target_res = gt[ind]
                self.word_acc_recorder[i].update(
                            (predict_res==target_res).sum().item(), 
                            predict_res.size(0), 
                            multiply=False
                    )                    

    def get_loss(self, results, epoch=-1):
        # input:
        #   - results:  contains the forward results of the model and some ground-truths
        # output:
        #   - loss:     to tune model's parameters
        if epoch != -1:
            rate = min((epoch + 1)/10, 1)
            if self.bow_index != -1:
                self.scales[self.bow_index] = rate

        loss = []
        for i in range(self.num_loss):
            # prepare the predictions and its corresponding ground-truths
            pred = results[self.keys[i][0]]
            gt = results[self.keys[i][1]]
            #print(gt)
            #print(gt.max())

            # calculate i-th loss
            if isinstance(self.crit[i], DistillationLoss):
                num_sample = gt.size(0)
                if self.opt['na']:
                    i_loss = self.crit[i](pred, gt, results['pure_target'])
                else:
                    i_loss = self.crit[i](pred, gt, results[Constants.mapping['lang'][1]])
            else:
                num_sample, i_loss = self.check_and_cal(pred, gt, self.crit[i])

            # weighting i-th loss
            loss.append(i_loss * self.scales[i])

            # update the statistics of i-th loss
            self.loss_recorder[i].update(i_loss.item(), num_sample)

            # For non-autoregressive, calculate accuracy of the generated words
            if self.calculate_word_acc:
                self.check_and_cal_word_acc(results[Constants.mapping['lang'][0]], results[Constants.mapping['lang'][1]])

        # loss = loss1 * scale1 + loss2 * scale2 + ...    
        loss = torch.stack(loss, dim=0).sum(0)
        return loss

    def get_loss_info(self):
        # standard operations:
        #   1. before a epoch, Criterion.reset_loss_recorder()
        #   2. during a epoch, Criterion.get_loss(...)
        #   3. after  a epoch, Criterion.get_loss_info()

        loss_info = [meter.avg for meter in self.loss_recorder]
        if self.calculate_word_acc:
            names = self.names + ['Word Acc%d' % i for i in range(self.calculate_word_acc)]
            loss_info = loss_info + [meter.avg for meter in self.word_acc_recorder]
        else:
            names = self.names
        return names, loss_info # e.g., CAP_LOSS: 0.1


def get_criterion(opt):
    crit_mapping = {
        'lang': CrossEntropyLoss(ignore_index=Constants.PAD),
        'obj': nn.BCELoss(),
        'ce': CrossEntropyLoss(),
        'tag': CrossEntropyLoss(ignore_index=Constants.PAD),
        'length': nn.SmoothL1Loss() if not opt.get('use_kl', False) else nn.KLDivLoss(),
        'bow': BagOfWordsLoss(ignore_index=Constants.PAD),
        'attr': nn.BCELoss(),
        'attr2': AttributeLoss(),
        'dist': DistillationLoss(ignore_index=Constants.PAD)
    }
    if opt.get('bow_loss', False):
        opt['crit'].append('bow')
        opt['crit_name'].append('BOW Loss')
        opt['crit_key'].append(Constants.mapping['bow'])
        opt['crit_scale'].append(0.1)

    crit_type = opt['crit']
    assert isinstance(crit_type, list)
    
    crit = []
    for item in crit_type:
        assert item.lower() in crit_mapping
        crit.append(crit_mapping[item.lower()])

    if opt['na']:
        if opt['method'] == 'mp':
            calculate_word_acc = 1
        elif opt['method'] == 'direct':
            calculate_word_acc = 3
        elif opt['method'] == 'signal':
            calculate_word_acc = 3
        elif opt['method'] == 'signal3':
            calculate_word_acc = 3
        elif opt['method'] == 'signal2':
            calculate_word_acc = 2
        elif opt['method'] == 'nv':
            calculate_word_acc = 2
        elif opt['method'] == 'ms':
            calculate_word_acc = 2
    else:
        if opt['method'] == 'ag':
            calculate_word_acc = 2
        else:
            calculate_word_acc = 0

    return Criterion(
            crit=crit,
            keys=opt['crit_key'],
            names=opt['crit_name'],
            scales=opt['crit_scale'],
            calculate_word_acc=calculate_word_acc,
            opt=opt
        )
