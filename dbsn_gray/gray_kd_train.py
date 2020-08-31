#
import os
import random
import datetime
import time
from pathlib import Path
import math
from scipy.optimize import curve_fit

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from data import create_dataset

from gray_kd_options import opt
from net.backbone_net import DBSN_Model
from net.sigma_net import Sigma_mu_Net, Sigma_n_Net
from net.mwcnn_gray import MWCNN
from util.utils import batch_psnr,findLastCheckpoint


seed=0
random.seed(seed)
np.random.seed(seed)
if torch.cuda.is_available():
    torch.cuda.manual_seed_all(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
torch.cuda.manual_seed_all(seed)
if seed == 0:
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


"""
# split the input data into two sub-sets: S1&S2
# S1: (x_i, y_i) using y_i only, to generate denoised image
# S2: (x_j, y_j) using x_j only, to generate noisy image
""" 
# Advanced loss:
class DistillationLoss(nn.Module):
    def __init__(self):
        super(DistillationLoss,self).__init__()
        self.L2=nn.MSELoss(reduction='mean')

    def forward(self, y_i_p, x_i_star, y_j_p, x_j):
        loss=0
        x_i_star = x_i_star.detach()
        x_j = x_j.detach()
        #
        lamb1 = 0.1
        lamb2 = 1
        t1 = self.L2(x_i_star,y_i_p)
        t2 = self.L2(x_j,y_j_p)
        loss = lamb1*t1 + lamb2*t2
        return loss 

def main(args):
    # Init loggers
    os.makedirs(args.log_dir, exist_ok=True)
    if args.noise_type == 'gaussian':
        if len(args.train_noiseL)>1:
            noise_level = 'nL'+str(int(args.train_noiseL[0]))+','+str(int(args.train_noiseL[1]))
        else:
            noise_level = 'nL'+str(int(args.train_noiseL[0]))
    elif args.noise_type == 'poisson_gaussian':
        if len(args.train_noiseL)>2:
            noise_level = 'sigmaS'+str(int(args.train_noiseL[1]))+'_sigmaC'+str(int(args.train_noiseL[2]))
        else:
            noise_level = 'sigmaS'+str(int(args.train_noiseL[0]))+'_sigmaC'+str(int(args.train_noiseL[1])) 
    else:
        raise ValueError('Noise_type [%s] is not found.' % (args.noise_type))    
    checkpoint_dir = args.save_prefix + '_' + noise_level
    ckpt_save_path = os.path.join(args.log_dir, checkpoint_dir)
    os.makedirs(ckpt_save_path, exist_ok=True)
    logger_fname = os.path.join(ckpt_save_path, checkpoint_dir+'_log.txt')


    with open(logger_fname, "w") as log_file:
        now = time.strftime("%c")
        log_file.write('================ Log output (%s) ================\n' % now) 
        log_file.write('Parameters \n') 
        for key in opt.__dict__:
            p = key+': '+str(opt.__dict__[key])
            log_file.write('%s\n' % p)

    # set training set
    train_setname = args.trainset
    dataset = create_dataset(train_setname, 'train', args).load_data()
    dataset_num = len(dataset)
    # set val set
    val_setname = args.valset
    dataset_val = create_dataset(val_setname, 'val', args).load_data()
    # logging
    with open(logger_fname, "a") as log_file:
        log_file.write('training/val dataset created\n')
        log_file.write('number of training examples {0} \n'.format(dataset_num))

    # net architecture
    dbsn_net = DBSN_Model(in_ch = args.input_channel,
                            out_ch = args.output_channel,
                            mid_ch = args.middle_channel,
                            blindspot_conv_type = args.blindspot_conv_type,
                            blindspot_conv_bias = args.blindspot_conv_bias,
                            br1_block_num = args.br1_block_num,
                            br1_blindspot_conv_ks =args.br1_blindspot_conv_ks,
                            br2_block_num = args.br2_block_num,
                            br2_blindspot_conv_ks = args.br2_blindspot_conv_ks,
                            activate_fun = args.activate_fun)
    sigma_mu_net = Sigma_mu_Net(in_ch=args.middle_channel,
                    out_ch=args.sigma_mu_output_channel,
                    mid_ch=args.sigma_mu_middle_channel,
                    layers=args.sigma_mu_layers,
                    kernel_size=args.sigma_mu_kernel_size,
                    bias=args.sigma_mu_bias)
    sigma_n_net = Sigma_n_Net(in_ch=args.sigma_n_input_channel,
            out_ch=args.sigma_n_output_channel,
            mid_ch=args.sigma_n_middle_channel,
            layers=args.sigma_n_layers,
            kernel_size=args.sigma_n_kernel_size,
            bias=args.sigma_n_bias)
    cnn_denoiser_net = MWCNN()

    # loss function
    criterion = DistillationLoss().cuda()
    
    # Move to GPU
    dbsn_model = nn.DataParallel(dbsn_net, args.device_ids).cuda()
    sigma_mu_model = nn.DataParallel(sigma_mu_net, args.device_ids).cuda()
    sigma_n_model = nn.DataParallel(sigma_n_net, args.device_ids).cuda()
    cnn_denoiser_model = cnn_denoiser_net.cuda()

    # new or continue
    ckpt_save_prefix = args.save_prefix + '_ckpt_e'
    initial_epoch = findLastCheckpoint(save_dir=ckpt_save_path, save_pre = ckpt_save_prefix)
    if initial_epoch > 0:
        print('*****resuming by loading epoch %03d' % initial_epoch)
        args.resume = "continue"
        args.last_ckpt = os.path.join(ckpt_save_path, ckpt_save_prefix + str(initial_epoch) + '.pth')

    # Optimizer
    training_params = None
    optimizer_cnn_denoiser = None
    if args.resume == "continue":
        tmp_ckpt=torch.load(args.last_ckpt,map_location=torch.device('cuda', args.device_ids[0]))
        training_params = tmp_ckpt['training_params']
        start_epoch = training_params['start_epoch']
        # Initialize dbsn_model
        pretrained_dict=tmp_ckpt['state_dict_dbsn']
        model_dict=dbsn_model.state_dict()
        pretrained_dict_update = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        assert(len(pretrained_dict)==len(pretrained_dict_update))
        assert(len(pretrained_dict_update)==len(model_dict))
        model_dict.update(pretrained_dict_update)
        dbsn_model.load_state_dict(model_dict)
        # Initialize sigma_mu_model
        pretrained_dict=tmp_ckpt['state_dict_sigma_mu']
        model_dict=sigma_mu_model.state_dict()
        pretrained_dict_update = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        assert(len(pretrained_dict)==len(pretrained_dict_update))
        assert(len(pretrained_dict_update)==len(model_dict))
        model_dict.update(pretrained_dict_update)
        sigma_mu_model.load_state_dict(model_dict)
        # Initialize sigma_n_model
        pretrained_dict=tmp_ckpt['state_dict_sigma_n']
        model_dict=sigma_n_model.state_dict()
        pretrained_dict_update = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        assert(len(pretrained_dict)==len(pretrained_dict_update))
        assert(len(pretrained_dict_update)==len(model_dict))
        model_dict.update(pretrained_dict_update)
        sigma_n_model.load_state_dict(model_dict)
        # Initialize cnn_denoiser_model
        pretrained_dict=tmp_ckpt['state_dict_cnn_denoiser']
        model_dict=cnn_denoiser_model.state_dict() 
        pretrained_dict_update = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        assert(len(pretrained_dict)==len(pretrained_dict_update))
        assert(len(pretrained_dict_update)==len(model_dict))
        model_dict.update(pretrained_dict_update)
        cnn_denoiser_model.load_state_dict(model_dict)
        optimizer_cnn_denoiser = optim.Adam(cnn_denoiser_model.parameters(), lr=args.lr_cnn_denoiser)
        optimizer_cnn_denoiser.load_state_dict(tmp_ckpt['optimizer_state_cnn_denoiser'])
        schedule_cnn_denoiser = torch.optim.lr_scheduler.MultiStepLR(optimizer_cnn_denoiser, milestones=args.steps, gamma=args.decay_rate)

    elif args.resume == "new":
        training_params = {}
        training_params['step'] = 0
        start_epoch = 0
        
        tmp_ckpt=torch.load(args.dbsn_ckpt,map_location=torch.device('cuda', args.device_ids[0]))
        # Initialize dbsn_model
        pretrained_dict=tmp_ckpt['state_dict_dbsn']
        model_dict=dbsn_model.state_dict()
        pretrained_dict_update = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        assert(len(pretrained_dict)==len(pretrained_dict_update))
        assert(len(pretrained_dict_update)==len(model_dict))
        model_dict.update(pretrained_dict_update)
        dbsn_model.load_state_dict(model_dict)
        # Initialize sigma_mu_model
        pretrained_dict=tmp_ckpt['state_dict_sigma_mu']
        model_dict=sigma_mu_model.state_dict()
        pretrained_dict_update = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        assert(len(pretrained_dict)==len(pretrained_dict_update))
        assert(len(pretrained_dict_update)==len(model_dict))
        model_dict.update(pretrained_dict_update)
        sigma_mu_model.load_state_dict(model_dict)
        # Initialize sigma_n_model
        pretrained_dict=tmp_ckpt['state_dict_sigma_n']
        model_dict=sigma_n_model.state_dict()
        pretrained_dict_update = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        assert(len(pretrained_dict)==len(pretrained_dict_update))
        assert(len(pretrained_dict_update)==len(model_dict))
        model_dict.update(pretrained_dict_update)
        sigma_n_model.load_state_dict(model_dict)
        
        if args.isPretrain:
            # Initialize dn_model
            tmp_ckpt=torch.load(args.pretrained_cnn_denoiser_path)
            pretrained_dict = tmp_ckpt['state_dict']
            model_dict=cnn_denoiser_model.state_dict()
            pretrained_dict_update = {k: v for k, v in pretrained_dict.items() if k in model_dict}
            assert(len(pretrained_dict)==len(pretrained_dict_update))
            assert(len(pretrained_dict_update)==len(model_dict))
            model_dict.update(pretrained_dict_update)
            cnn_denoiser_model.load_state_dict(model_dict)
            #
            cnn_denoiser_model.eval()
            with torch.no_grad():
                psnr_val_cnn_denoiser = 0
                for count, data in enumerate(dataset_val):
                    # input
                    img_val = data['clean'].cuda()
                    img_noise_val = data['noisy'].cuda()
                    # crop the input (8x MWCNN)
                    img_val = img_val[:,:,:-1,:-1]
                    img_noise_val = img_noise_val[:,:,:-1,:-1]
                    _,C,H,W = img_noise_val.shape
                    # forward
                    cnn_denoiser_out_val = cnn_denoiser_model(img_noise_val)
                    psnr_cnn_denoiser = batch_psnr(cnn_denoiser_out_val.clamp(0., 1.), img_val.clamp(0., 1.), 1.)
                    psnr_val_cnn_denoiser+=psnr_cnn_denoiser
                psnr_val_cnn_denoiser /= len(dataset_val)
                print("Pre-trained CNN denoiser on BSD68, avg psnr: %.4f \n" % (psnr_val_cnn_denoiser))
        #
        optimizer_cnn_denoiser = optim.Adam(cnn_denoiser_model.parameters(), lr=args.lr_cnn_denoiser)
        schedule_cnn_denoiser = torch.optim.lr_scheduler.MultiStepLR(optimizer_cnn_denoiser, milestones=args.steps, gamma=args.decay_rate)        


    # logging
    dbsn_params = sum(param.numel() for param in dbsn_net.parameters() if param.requires_grad)/(1e6)
    sigma_mu_params = sum(param.numel() for param in sigma_mu_net.parameters() if param.requires_grad)/(1e3)
    sigma_n_params = sum(param.numel() for param in sigma_n_net.parameters() if param.requires_grad)/(1e3)
    cnn_denoiser_params = sum(param.numel() for param in cnn_denoiser_net.parameters() if param.requires_grad)/(1e6)
    with open(logger_fname, "a") as log_file:
        log_file.write('model created\n')
        log_file.write('DBSN trainable parameters: {dbsn_params:.2f}M\n'.format(dbsn_params=dbsn_params))
        log_file.write('SigmaMuNet trainable parameters: {sigma_mu_params:.2f}K\n'.format(sigma_mu_params=sigma_mu_params))
        log_file.write('SigmaNNet trainable parameters: {sigma_n_params:.2f}K\n'.format(sigma_n_params=sigma_n_params))
        log_file.write('CNNDenoiser trainable parameters: {cnn_denoiser_params:.2f}M\n'.format(cnn_denoiser_params=cnn_denoiser_params))


    # --------------------------------------------
    # Checkpoint
    # --------------------------------------------
    if args.resume == 'continue':
        # # evaluating the loaded model first ...
        # print("Starting from epoch: %d "%(start_epoch))
        # logging
        with open(logger_fname, "a") as log_file:
            log_file.write('checkpoint evaluation on epoch {0} ... \n'.format(start_epoch))
        dbsn_model.eval()
        sigma_mu_model.eval()
        sigma_n_model.eval()
        cnn_denoiser_model.eval()
        with torch.no_grad():
            psnr_val = 0
            psnr_val_dbsn = 0
            psnr_val_cnn_denoiser = 0
            for count, data in enumerate(dataset_val):
                # input
                img_val = data['clean'].cuda()
                img_noise_val = data['noisy'].cuda()
                # crop the input (8x MWCNN)
                img_val = img_val[:,:,:-1,:-1]
                img_noise_val = img_noise_val[:,:,:-1,:-1]
                _,C,H,W = img_noise_val.shape
                # forward
                cnn_denoiser_out_val = cnn_denoiser_model(img_noise_val)
                mu_out_val, mid_out_val = dbsn_model(img_noise_val)
                sigma_mu_out_val = sigma_mu_model(mid_out_val)
                # 
                sigma_mu_val = sigma_mu_out_val ** 2
                if args.noise_type == 'gaussian':
                    sigma_n_out_val = sigma_n_model(img_noise_val)
                    sigma_n_out_val = sigma_n_out_val.mean(dim=(2,3), keepdim=True).repeat(1,1,H,W)
                else:
                    sigma_n_out_val = sigma_n_model(mu_out_val)
                noise_est_val = F.softplus(sigma_n_out_val - 4) + (1e-3)
                sigma_n_val = noise_est_val ** 2
                map_out_val = (img_noise_val * sigma_mu_val + mu_out_val * sigma_n_val) / (sigma_mu_val + sigma_n_val)
                # compute PSNR
                psnr_mu = batch_psnr(mu_out_val.clamp(0., 1.), img_val.clamp(0., 1.), 1.)
                psnr_val+=psnr_mu
                psnr_dbsn = batch_psnr(map_out_val.clamp(0., 1.), img_val.clamp(0., 1.), 1.)
                psnr_val_dbsn+=psnr_dbsn
                psnr_cnn_denoiser = batch_psnr(cnn_denoiser_out_val.clamp(0., 1.), img_val.clamp(0., 1.), 1.)
                psnr_val_cnn_denoiser+=psnr_cnn_denoiser
                # print
                print("Image[%d]: psnr_mu=%.4f, psnr_dbsn=%.4f, psnr_cnn_denoiser=%.4f " % (count, psnr_mu, psnr_dbsn, psnr_cnn_denoiser))
                # logging
                with open(logger_fname, "a") as log_file:
                    log_file.write('Image {0} \t' 
                        'psnr_mu:{psnr_mu:.4f} \t'
                        'psnr_dbsn:{psnr_dbsn:.4f} \n'
                        'psnr_cnn_denoiser:{psnr_cnn_denoiser:.4f} \n'.format(count, psnr_mu=psnr_mu,psnr_dbsn=psnr_dbsn,psnr_cnn_denoiser=psnr_cnn_denoiser))
            psnr_val /= len(dataset_val)
            psnr_val_dbsn /= len(dataset_val)
            psnr_val_cnn_denoiser /= len(dataset_val)
            # print
            print("Checkpoint avg psnr_mu: %.4f, avg psnr_dbsn: %.4f, avg psnr_cnn_denoiser: %.4f \n" % (psnr_val, psnr_val_dbsn, psnr_val_cnn_denoiser))
            # logging
            with open(logger_fname, "a") as log_file:
                log_file.write('checkpoint avg psnr_mu:{psnr_val:.4f} \t'
                'avg psnr_dbsn:{psnr_val_dbsn:.4f} \t'
                'avg psnr_cnn_denoiser:{psnr_val_cnn_denoiser:.4f} \n\n'.format(psnr_val=psnr_val, psnr_val_dbsn=psnr_val_dbsn, psnr_val_cnn_denoiser=psnr_val_cnn_denoiser))
            
            val_psnr_pre = psnr_val_cnn_denoiser
            idx_epoch = start_epoch


    val_psnr_curr = 0
    # --------------------------------------------
    # Training
    # --------------------------------------------
    # logging
    with open(logger_fname, "a") as log_file:
        log_file.write('started training\n')     
    # training
    for epoch in range(start_epoch, args.epoch):
        epoch_start_time = time.time()
        #
        print('lr: %.10f' % (optimizer_cnn_denoiser.state_dict()["param_groups"][0]["lr"]))    
        # set training mode
        dbsn_model.eval()
        sigma_mu_model.eval()
        sigma_n_model.eval()
        cnn_denoiser_model.train()
        # train
        for i, data in enumerate(dataset):
            # input
            img_clean = data['clean'].cuda()
            img_noise = data['noisy'].cuda()
            #
            optimizer_cnn_denoiser.zero_grad()
            #
            batch_size, C, H, W = img_clean.size()
            assert( batch_size%8==0 )
            """
            # split the input data into two sub-sets: S1&S2
            # S1: (x_i, y_i) using y_i only, to generate denoised image
            # S2: (x_j, y_j) using x_j only, to generate noisy image
            """ 
            x_i = img_clean[:batch_size//8,:,:,:]
            y_i = img_noise[:batch_size//8,:,:,:]
            x_j = img_clean[batch_size//8:,:,:,:]
            y_j = img_noise[batch_size//8:,:,:,:]
            num_noisy = batch_size//8
            num_clean = batch_size - num_noisy
            # compute x_i_star
            with torch.no_grad():
                mu_out, mid_out = dbsn_model(y_i)
                sigma_mu_out = sigma_mu_model(mid_out)
                # process sigma_mu & sigma_n
                sigma_mu = sigma_mu_out ** 2
                #
                if args.noise_type == 'gaussian':
                    sigma_n_out = sigma_n_model(y_i)
                    sigma_n_out = sigma_n_out.mean(dim=(2,3),keepdim=True).repeat(1,1,H,W)
                else:
                    sigma_n_out = sigma_n_model(mu_out)
                noise_est = F.softplus(sigma_n_out - 4) + (1e-3)
                sigma_n = noise_est ** 2
                x_i_star = (y_i * sigma_mu + mu_out * sigma_n) / (sigma_mu + sigma_n)
                # generate y_j
                sigma_n_x_j = sigma_n_model(x_j)
                if args.noise_type == 'gaussian':
                    sigma_n_x_j = sigma_n_x_j.mean(dim=(2,3),keepdim=True).repeat(1,1,H,W)
                noise_est = F.softplus(sigma_n_x_j - 4) + (1e-3)
                sigma_n = (noise_est ** 2) ** 0.5
                disturb = torch.rand(1, device='cuda')*(1.1-0.9)+0.9
                sigma_tmp = torch.randn(x_j.shape, device='cuda')*sigma_n*disturb
                y_j = x_j + sigma_tmp
            # train cnn_denoiser with {y_i=img_noise, y_j}
            y_i_out = cnn_denoiser_model(y_i)
            y_j_out = cnn_denoiser_model(y_j)
            # compute loss
            loss = criterion(y_i_out, x_i_star, y_j_out, x_j) 
            loss = loss / (2*batch_size)
            loss_value = loss.item()
            # backward and update parameters
            loss.backward()
            optimizer_cnn_denoiser.step()
            # Results
            training_params['step'] += args.batch_size
            # print results
            if training_params['step'] % (args.batch_size*args.display_freq) == 0:
                #
                cnn_out = torch.cat((y_i_out,y_j_out),0)
                train_psnr = batch_psnr(cnn_out.clamp(0., 1.), img_clean.clamp(0., 1.), 1.0)

                print(("[epoch %d][%d/%d], loss:%.4f, psnr: %.4f") % (epoch, i*args.batch_size, len(dataset), loss_value, train_psnr))

        schedule_cnn_denoiser.step()
        # taking time for each epoch
        tr_take_time = time.time() - epoch_start_time

        # --------------------------------------------
        # validation
        # --------------------------------------------
        # print
        # print("Evaluating on "+str(val_setname[0]))
        # logging
        with open(logger_fname, "a") as log_file:
            log_file.write('Evaluation on %s \n' % (str(val_setname[0])) )
        # set evaluation mode
        dbsn_model.eval()
        sigma_mu_model.eval()
        sigma_n_model.eval()
        cnn_denoiser_model.eval()
        #
        val_start_time = time.time()
        with torch.no_grad():
            psnr_val = 0
            psnr_val_dbsn = 0
            psnr_val_cnn_denoiser = 0
            for count, data in enumerate(dataset_val):
                # input
                img_val = data['clean'].cuda()
                img_noise_val = data['noisy'].cuda()
                # crop the input (8x MWCNN)
                img_val = img_val[:,:,:-1,:-1]
                img_noise_val = img_noise_val[:,:,:-1,:-1]
                _,C,H,W = img_noise_val.shape
                # forward
                cnn_denoiser_out_val = cnn_denoiser_model(img_noise_val)
                mu_out_val, mid_out_val = dbsn_model(img_noise_val)
                sigma_mu_out_val = sigma_mu_model(mid_out_val)
                # 
                sigma_mu_val = sigma_mu_out_val ** 2
                if args.noise_type == 'gaussian':
                    sigma_n_out_val = sigma_n_model(img_noise_val)
                    sigma_n_out_val = sigma_n_out_val.mean(dim=(2,3), keepdim=True).repeat(1,1,H,W)
                else:
                    sigma_n_out_val = sigma_n_model(mu_out_val)
                noise_est_val = F.softplus(sigma_n_out_val - 4) + (1e-3)
                sigma_n_val = noise_est_val ** 2
                map_out_val = (img_noise_val * sigma_mu_val + mu_out_val * sigma_n_val) / (sigma_mu_val + sigma_n_val)
                # compute PSNR
                psnr_mu = batch_psnr(mu_out_val.clamp(0., 1.), img_val.clamp(0., 1.), 1.)
                psnr_val+=psnr_mu
                psnr_dbsn = batch_psnr(map_out_val.clamp(0., 1.), img_val.clamp(0., 1.), 1.)
                psnr_val_dbsn+=psnr_dbsn
                psnr_cnn_denoiser = batch_psnr(cnn_denoiser_out_val.clamp(0., 1.), img_val.clamp(0., 1.), 1.)
                psnr_val_cnn_denoiser+=psnr_cnn_denoiser
                # # print
                # print("Image[%d]: psnr_mu=%.4f, psnr_dbsn=%.4f, psnr_cnn_denoiser=%.4f" % (count, psnr_mu, psnr_dbsn, psnr_cnn_denoiser))
                # logging
                with open(logger_fname, "a") as log_file:
                    log_file.write('Image {0} \t' 
                        'psnr_mu:{psnr_mu:.4f} \t'
                        'psnr_dbsn:{psnr_dbsn:.4f} \n'
                        'psnr_cnn_denoiser:{psnr_cnn_denoiser:.4f} \n'.format(count, psnr_mu=psnr_mu,psnr_dbsn=psnr_dbsn,psnr_cnn_denoiser=psnr_cnn_denoiser))
            torch.cuda.synchronize()
            val_take_time = time.time() - val_start_time
            psnr_val /= len(dataset_val)
            psnr_val_dbsn /= len(dataset_val)
            psnr_val_cnn_denoiser /= len(dataset_val)
            # Record the best PSNR
            val_psnr_curr = psnr_val_cnn_denoiser
            if epoch==0:
                val_psnr_pre = val_psnr_curr

        # save model and checkpoint
        if val_psnr_curr >= val_psnr_pre or (epoch+1) % args.save_model_freq == 0:                    
            # save model and checkpoint
            savename = args.save_prefix
            training_params['start_epoch'] = epoch
            save_dict = {'state_dict_dbsn': dbsn_model.state_dict(),
                        'state_dict_sigma_mu': sigma_mu_model.state_dict(),
                        'state_dict_sigma_n': sigma_n_model.state_dict(),
                        'state_dict_cnn_denoiser': cnn_denoiser_model.state_dict(),
                        'optimizer_state_cnn_denoiser': optimizer_cnn_denoiser.state_dict(),
                        'schedule_state_cnn_denoiser': schedule_cnn_denoiser.state_dict(),
                        'training_params': training_params,
                        'args': args}
            torch.save(save_dict, os.path.join(ckpt_save_path, ckpt_save_prefix + '{}.pth'.format(epoch)))
            if val_psnr_curr >= val_psnr_pre:
                val_psnr_pre = val_psnr_curr
                idx_epoch = epoch
                torch.save(dbsn_model.state_dict(), os.path.join(ckpt_save_path, 'dbsn_net_best_e{}.pth'.format(epoch)))
                torch.save(sigma_mu_model.state_dict(), os.path.join(ckpt_save_path, 'sigma_mu_net_best_e{}.pth'.format(epoch)))
                torch.save(sigma_n_model.state_dict(), os.path.join(ckpt_save_path, 'sigma_n_net_best_e{}.pth'.format(epoch)))
                torch.save(cnn_denoiser_model.state_dict(), os.path.join(ckpt_save_path, 'cnn_denoiser_net_best_e{}.pth'.format(epoch)))
            del save_dict

        # print 
        print('Epoch [%d/%d/%d] \t Train_time: %d sec \t Val_time: %d sec \t' % 
            (epoch, args.epoch, idx_epoch, tr_take_time, val_take_time))
        print('Epoch [%d/%d/%d] \t VAL psnr_dbsn: %.4f \t VAL psnr_cnn_denoiser: %.4f \t Best VAL psnr_cnn_denoiser: %.4f \t' % 
            (epoch, args.epoch, idx_epoch, psnr_val_dbsn, psnr_val_cnn_denoiser, val_psnr_pre))            
        # logging
        with open(logger_fname, "a") as log_file:
            #[0/1/2]: 0-current epoch, 1-total epoch, 2-best epoch
            log_file.write('Epoch: [{0}/{1}/{2}] \t'
            'Train_time:{tr_take_time:.1f} sec \t'
            'Val_time:{val_take_time:.1f} sec \t'
            'VAL psnr_dbsnr:{psnr_val_dbsn:.4f} \t'
            'VAL psnr_cnn_denoiser:{psnr_val_cnn_denoiser:.4f} \t'
            'Best VAL psnr_cnn_denoiser:{val_psnr_pre:.4f} \n'.format(epoch, args.epoch, idx_epoch,
            tr_take_time=tr_take_time, val_take_time=val_take_time, 
            psnr_val_dbsn=psnr_val_dbsn, psnr_val_cnn_denoiser=psnr_val_cnn_denoiser,val_psnr_pre=val_psnr_pre))
    
if __name__ == "__main__":

    main(opt)

    exit(0)



