#coding=utf-8
import os
import math
import tqdm
import torch
import numpy as np
from torch import nn
import torchvision as tv
from sklearn import metrics
import torchvision.utils as vutils
from torch.nn import functional as F

from utils import visualization
from dataset import augmentations
from utils.losses import CountLoss


class Detector(object):
    def __init__(self, net, train_loader=None, test_loader=None, batch_size=None, 
                 optimizer='adam', lr=1e-3, patience=5, interval=1, num_classes=1, cov=1, 
                 checkpoint_dir='saved_models', checkpoint_name='', devices=[0], log_size=(96, 96)):
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.lr = lr
        self.batch_size = batch_size
        self.patience = patience
        self.interval = interval
        self.checkpoint_dir = checkpoint_dir
        self.checkpoint_name = checkpoint_name
        self.devices = devices
        self.scale = cov * math.pi * 2
        self.num_classes = num_classes
        self.log_size = log_size
        
        if not os.path.exists(checkpoint_dir):
            os.mkdir(checkpoint_dir)
            
        self.net_single = net
        if len(devices) == 0:
            pass
        elif len(devices) == 1:
            self.net = self.net_single.cuda()
        else:
            self.net = nn.DataParallel(self.net_single, device_ids=range(len(devices))).cuda()
            
        if optimizer == 'sgd':
            self.opt = torch.optim.SGD(
                self.net_single.parameters(), lr=lr, weight_decay=1e-6, momentum=0.9)
        elif optimizer == 'adam':
            self.opt = torch.optim.Adam(
                self.net_single.parameters(), lr=lr, weight_decay=1e-6)
        else:
            raise Exception('Optimizer {} Not Exists'.format(optimizer))

        self.criterion = CountLoss(self.scale)
        
    def reset_grad(self):
        self.opt.zero_grad()
        
    def train(self, max_epoch, writer=None, epoch_size=100):
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(self.opt, max_epoch * epoch_size)
        torch.cuda.manual_seed(1)
        best_score = 1000
        step = 1
        for epoch in tqdm.tqdm(range(max_epoch), total=max_epoch):
            torch.cuda.empty_cache()
            self.net.train()
            for batch_idx, data in enumerate(self.train_loader):
                img = data['image'].cuda()
                hm = data['heatmap'].cuda()
                mask = data['mask'].cuda()
                num = data['num'].cuda()

                self.reset_grad()
                pred_hm, pred_mask = self.net(img)
                loss = self.get_loss((pred_hm, pred_mask), (hm, mask, num))
                loss.backward()
                self.opt.step()
                if writer:
                    writer.add_scalar(
                        'loss', loss.data, global_step=step)
                    writer.add_scalar(
                        'lr', self.opt.param_groups[0]['lr'], global_step=step)
                step += 1
                scheduler.step(step)
                
            if epoch % self.interval == 0:
                torch.cuda.empty_cache()
                total_loss, imgs, pred_hms, gt_hms, pred_masks, gt_masks = self.test()
                if writer:
                    writer.add_scalar(
                        'Test Loss', total_loss, global_step=epoch)
                    score = total_loss
                    
                    pred_hms = self.draw_heatmap(imgs, pred_hms)
                    writer.add_image('Pred HM', pred_hms, epoch)
                    
                    gt_hms = self.draw_heatmap(imgs, gt_hms)
                    writer.add_image('GT HM', gt_hms, epoch)
                    
                    pred_masks = self.draw_heatmap(imgs, pred_masks)
                    writer.add_image('Pred Mask', pred_masks, epoch)
                    
                    gt_masks = self.draw_heatmap(imgs, gt_masks)
                    writer.add_image('GT Mask', gt_masks, epoch)
                    
                if best_score >= score:
                    best_score = score
                    self.save_model(self.checkpoint_dir)

    def test(self):
        self.net.eval()
        with torch.no_grad():
            imgs = []
            gt_hms = []
            pred_hms = []
            gt_masks = []
            pred_masks = []
            total_loss = 0
            count = 0
            for batch_idx, data in enumerate(self.test_loader):
                img = data['image'].cuda()
                hm = data['heatmap']
                mask = data['mask']
                num = data['num']
                
                pred_hm, pred_mask = self.net(img)
                
                pred_hm = torch.sigmoid(pred_hm).detach().cpu()
                pred_mask = pred_mask.detach().cpu()

                img = img.cpu()
                imgs.append(img)
                pred_hms.append(pred_hm)
                gt_hms.append(hm)
            
                pred_masks.append(pred_mask)
                gt_masks.append(mask)

                count += img.shape[0]
                if count >= 40:
                    break
            gt_hms = torch.cat(gt_hms)
            pred_hms = torch.cat(pred_hms)
            gt_masks = torch.cat(gt_masks)
            pred_masks = torch.cat(pred_masks)
            imgs = torch.cat(imgs)
            total_loss /= batch_idx + 1
        return total_loss, imgs[: 40], pred_hms[: 40], gt_hms[: 40], pred_masks[: 40], gt_masks[: 40]

    def draw_heatmap(self, img, hm, size=None):
        if size is None:
            size = self.log_size
        img = F.interpolate(img, size)
        img = vutils.make_grid(img).numpy()
        rgb = visualization.heatmap_to_rgb(hm, self.num_classes, size)
        result = np.clip((rgb + img) / 2, 0, 1)
        return result
    
    def draw_mask(self, img, mask, size=None, is_gt=False):
        if size is None:
            size = self.log_size
        img = F.interpolate(img, size)
        img = vutils.make_grid(img).numpy()
        rgb = visualization.mask_to_rgb(mask, self.num_classes, size, is_gt=False)
        result = np.clip((rgb + img) / 2, 0, 1)
        return result

    def save_model(self, checkpoint_dir, comment=None):
        if comment is None:
            torch.save(self.net_single, '{}/best_model_{}.pt'.format(checkpoint_dir, self.checkpoint_name))
        else:
            torch.save(self.net_single, '{}/best_model_{}_{}.pt'.format(checkpoint_dir, self.checkpoint_name, comment))
            
    def load_model(self, model_path):
        self.net_single.load_state_dict(torch.load(model_path).state_dict())
    
    def predict(self, img):
        x = torch.from_numpy(img).type(torch.float32).permute(0, 3, 1, 2).cuda() / 255
        self.net.eval()
        with torch.no_grad():
            pred_hm, pred_mask = self.net(x)
            pred_hm = torch.sigmoid(pred_hm).detach().cpu().numpy()
            pred_mask = pred_mask.argmax(dim=1).detach().cpu().numpy()
        return pred_hm, pred_mask
    
    def get_loss(self, pred, target):
        loss = self.criterion(pred, target)
        return loss