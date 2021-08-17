import time
import argparse
import datetime
import sys
import os
import logging

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.utils as utils

import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp

from tensorboardX import SummaryWriter

import matplotlib
import matplotlib.cm
import threading
from tqdm import tqdm
from utils import DiceLoss

from dataloader import *
from models.model import VisionTransformer as ViT_seg
from models.model import CONFIGS as CONFIGS_ViT_seg
from models.model import *
from plotgraph import plotgraph

def convert_arg_line_to_args(arg_line):
    for arg in arg_line.split():
        if not arg.strip():
            continue
        yield arg

parser = argparse.ArgumentParser(description="BTS PyTorch implementation.", fromfile_prefix_chars="@")
parser.convert_arg_line_to_args = convert_arg_line_to_args

parser.add_argument("--mode",                      type=str,   help="train or test", default="train")
parser.add_argument("--model_name",                type=str,   help="model name", default="bts_eigen_v2")
# parser.add_argument("--encoder",                   type=str,   help="type of encoder, desenet121_bts, densenet161_bts, "
#                                                                     "resnet101_bts, resnet50_bts, resnext50_bts or resnext101_bts",
#                                                                default="densenet161_bts")
# Dataset
parser.add_argument("--dataset",                   type=str,   help="dataset to train on, kitti or nyu", default="kitti")
parser.add_argument("--data_path",                 type=str,   help="path to the data", required=True)
parser.add_argument("--gt_path",                   type=str,   help="path to the groundtruth data", required=True)
parser.add_argument("--filenames_file",            type=str,   help="path to the filenames text file", required=True)
parser.add_argument("--input_height",              type=int,   help="input image height", default=480)
parser.add_argument("--input_width",               type=int,   help="input image width",  default=640)
parser.add_argument("--max_depth",                 type=float, help="maximum depth in estimation", default=10)

# # Log and save
parser.add_argument("--log_directory",             type=str,   help="directory to save checkpoints and summaries", default="")
parser.add_argument("--checkpoint_path",           type=str,   help="path to a checkpoint to load", default="")
parser.add_argument("--log_freq",                  type=int,   help="Logging frequency in global steps", default=100)
parser.add_argument("--save_freq",                 type=int,   help="Checkpoint saving frequency in global steps", default=500)

# # Training
parser.add_argument("--fix_first_conv_blocks",                 help="if set, will fix the first two conv blocks", action="store_true")
parser.add_argument("--fix_first_conv_block",                  help="if set, will fix the first conv block", action="store_true")
parser.add_argument("--bn_no_track_stats",                     help="if set, will not track running stats in batch norm layers", action="store_true")
parser.add_argument("--weight_decay",              type=float, help="weight decay factor for optimization", default=1e-2)
parser.add_argument("--bts_size",                  type=int,   help="initial num_filters in bts", default=512)
parser.add_argument("--retrain",                               help="if used with checkpoint_path, will restart training from step zero", action="store_true")
parser.add_argument("--adam_eps",                  type=float, help="epsilon in Adam optimizer", default=1e-6)
parser.add_argument("--batch_size",                type=int,   help="batch size", default=4)
parser.add_argument("--num_epochs",                type=int,   help="number of epochs", default=50)
parser.add_argument("--learning_rate",             type=float, help="initial learning rate", default=1e-4)
parser.add_argument("--end_learning_rate",         type=float, help="end learning rate", default=-1)
parser.add_argument("--variance_focus",            type=float, help="lambda in paper: [0, 1], higher value more focus on minimizing variance of error", default=0.85)

# # Preprocessing
parser.add_argument("--do_random_rotate",                      help="if set, will perform random rotation for augmentation", action="store_true")
parser.add_argument("--do_random_crop",                             help="if set, will perform random crop for augmentation", action="store_true")
parser.add_argument("--degree",                    type=float, help="random rotation maximum degree", default=2.5)
parser.add_argument("--do_kb_crop",                            help="if set, crop input images as kitti benchmark images", action="store_true")
parser.add_argument("--use_right",                             help="if set, will randomly use right images when train on KITTI", action="store_true")

# # Multi-gpu training
parser.add_argument("--num_threads",               type=int,   help="number of threads to use for data loading", default=1)
parser.add_argument("--world_size",                type=int,   help="number of nodes for distributed training", default=1)
parser.add_argument("--rank",                      type=int,   help="node rank for distributed training", default=0)
parser.add_argument("--dist_url",                  type=str,   help="url used to set up distributed training", default="tcp://127.0.0.1:1234")
parser.add_argument("--dist_backend",              type=str,   help="distributed backend", default="gloo") # default="nccl")
parser.add_argument("--gpu",                       type=int,   help="GPU id to use.", default=None)
parser.add_argument("--multiprocessing_distributed",           help="Use multi-processing distributed training to launch "
                                                                    "N processes per node, which has N GPUs. This is the "
                                                                    "fastest way to use PyTorch for either single node or "
                                                                    "multi node data parallel training", action="store_true",)
# # Online eval
parser.add_argument("--do_online_eval",                        help="if set, perform online eval in every eval_freq steps", action="store_true")
parser.add_argument("--data_path_eval",            type=str,   help="path to the data for online evaluation", required=False)
parser.add_argument("--gt_path_eval",              type=str,   help="path to the groundtruth data for online evaluation", required=False)
parser.add_argument("--filenames_file_eval",       type=str,   help="path to the filenames text file for online evaluation", required=False)
parser.add_argument("--min_depth_eval",            type=float, help="minimum depth for evaluation", default=1e-3)
parser.add_argument("--max_depth_eval",            type=float, help="maximum depth for evaluation", default=80)
parser.add_argument("--eigen_crop",                            help="if set, crops according to Eigen NIPS14", action="store_true")
parser.add_argument("--garg_crop",                             help="if set, crops according to Garg  ECCV16", action="store_true")
parser.add_argument("--eval_freq",                 type=int,   help="Online evaluation frequency in global steps", default=500)
parser.add_argument("--eval_summary_directory",    type=str,   help="output directory for eval summary,"
                                                                    "if empty outputs to checkpoint folder", default="")

# # # TransUnet/TransUNet args
parser.add_argument("--num_classes", type=int,
                    default=1, help="output channel of network")
parser.add_argument("--max_epochs", type=int,
                    default=150, help="maximum epoch number to train")
parser.add_argument("--n_gpu", type=int, default=1, help="total gpu")
parser.add_argument("--base_lr", type=float,  default=0.01,
                    help="segmentation network learning rate")
parser.add_argument("--img_size_height", type=int,
                    default=352, help="input patch size of network input")
parser.add_argument("--img_size_width", type=int,
                    default=704, help="input patch size of network input")  # make it the same param with the upper input_*
parser.add_argument("--n_skip", type=int,
                    default=3, help="using number of skip-connect, default is num")
parser.add_argument("--vit_name", type=str,
                    default="R50-ViT-L_16", help="select one vit model")
parser.add_argument("--patches_size", type=int,
                    default=16, help="patches_size, default is 16")


if sys.argv.__len__() == 2:
    arg_filename_with_prefix = "@" + sys.argv[1]
    args = parser.parse_args([arg_filename_with_prefix])
else:
    args = parser.parse_args()

if args.mode == "train" and not args.checkpoint_path:
    from models.model import *

elif args.mode == "train" and args.checkpoint_path:
    model_dir = os.path.dirname(args.checkpoint_path)
    model_name = os.path.basename(model_dir)
    import sys
    sys.path.append(model_dir)
    for key, val in vars(__import__(model_name)).items():
        if key.startswith("__") and key.endswith("__"):
            continue
        vars()[key] = val


inv_normalize = transforms.Normalize(
    mean=[-0.485/0.229, -0.456/0.224, -0.406/0.225],
    std=[1/0.229, 1/0.224, 1/0.225]
)

eval_metrics = ["silog", "abs_rel", "log10", "rms", "sq_rel", "log_rms", "d1", "d2", "d3"]


def compute_errors(gt, pred):
    thresh = np.maximum((gt / pred), (pred / gt))
    d1 = (thresh < 1.25).mean()
    d2 = (thresh < 1.25 ** 2).mean()
    d3 = (thresh < 1.25 ** 3).mean()

    rms = (gt - pred) ** 2
    rms = np.sqrt(rms.mean())

    log_rms = (np.log(gt) - np.log(pred)) ** 2
    log_rms = np.sqrt(log_rms.mean())

    abs_rel = np.mean(np.abs(gt - pred) / gt)
    sq_rel = np.mean(((gt - pred) ** 2) / gt)

    err = np.log(pred) - np.log(gt)
    silog = np.sqrt(np.mean(err ** 2) - np.mean(err) ** 2) * 100

    err = np.abs(np.log10(pred) - np.log10(gt))
    log10 = np.mean(err)

    return [silog, abs_rel, log10, rms, sq_rel, log_rms, d1, d2, d3]


def block_print():
    sys.stdout = open(os.devnull, "w")


def enable_print():
    sys.stdout = sys.__stdout__


def get_num_lines(file_path):
    f = open(file_path, "r")
    lines = f.readlines()
    f.close()
    return len(lines)


def colorize(value, vmin=None, vmax=None, cmap="Greys"):
    value = value.cpu().numpy()[:, :, :]
    value = np.log10(value)

    vmin = value.min() if vmin is None else vmin
    vmax = value.max() if vmax is None else vmax

    if vmin != vmax:
        value = (value - vmin) / (vmax - vmin)
    else:
        value = value*0.

    cmapper = matplotlib.cm.get_cmap(cmap)
    value = cmapper(value, bytes=True)

    img = value[:, :, :3]

    return img.transpose((2, 0, 1))


def normalize_result(value, vmin=None, vmax=None):
    value = value.cpu().numpy()[0, :, :]

    vmin = value.min() if vmin is None else vmin
    vmax = value.max() if vmax is None else vmax

    if vmin != vmax:
        value = (value - vmin) / (vmax - vmin)
    else:
        value = value * 0.

    return np.expand_dims(value, 0)

"""
def set_misc(model):
    if args.bn_no_track_stats:
        print("Disabling tracking running stats in batch norm layers")
        model.apply(bn_init_as_tf)

    if args.fix_first_conv_blocks:
        if "resne" in args.encoder:
            fixing_layers = ["base_model.conv1", "base_model.layer1.0", "base_model.layer1.1", ".bn"]
        else:
            fixing_layers = ["conv0", "denseblock1.denselayer1", "denseblock1.denselayer2", "norm"]
        print("Fixing first two conv blocks")
    elif args.fix_first_conv_block:
        if "resne" in args.encoder:
            fixing_layers = ["base_model.conv1", "base_model.layer1.0", ".bn"]
        else:
            fixing_layers = ["conv0", "denseblock1.denselayer1", "norm"]
        print("Fixing first conv block")
    else:
        if "resne" in args.encoder:
            fixing_layers = ["base_model.conv1", ".bn"]
        else:
            fixing_layers = ["conv0", "norm"]
        print("Fixing first conv layer")

    for name, child in model.named_children():
        if not "encoder" in name:
            continue
        for name2, parameters in child.named_parameters():
            # print(name, name2)
            if any(x in name2 for x in fixing_layers):
                parameters.requires_grad = False
"""


"""
# check loaded data(kitti)
import matplotlib.pyplot as plt
import numpy as np
def custom_imshow(img): 
    img = img.cpu().numpy()
    plt.imshow(np.transpose(img, (1, 2, 0))) 
    plt.show()
def check_dataloader(): 

    args.distributed = False
    dataloader = BtsDataLoader(args, "train")
    dataloader_eval = BtsDataLoader(args, "online_eval")

    
    global_step = 0
    steps_per_epoch = len(dataloader.data)
    num_total_steps = args.num_epochs * steps_per_epoch
    epoch = global_step // steps_per_epoch


    
    maximum = 0
    # for batch_idx, sample_batched in enumerate(dataloader.data): 
    for sample_batched in tqdm(dataloader.data):
    
        image = torch.autograd.Variable(sample_batched["image"].cuda(args.gpu, non_blocking=True))
        focal = torch.autograd.Variable(sample_batched["focal"].cuda(args.gpu, non_blocking=True))
        depth_gt = torch.autograd.Variable(sample_batched["depth"].cuda(args.gpu, non_blocking=True))


        # print("batch_idx:", batch_idx)
        # print("shape:", image.shape)  # (batch, 3, 352, 704)
        # print("focal:", focal.shape)  # (batch)
        # print("depth_gt:", depth_gt.shape)  # (batch, 1, 352, 704)
        # print("depth======:", depth_gt)

        gt_list = depth_gt.view(-1)
        gt_list = list(set(gt_list.cpu().numpy()))
        # print(gt_list)
        new = max(gt_list)

        if new > maximum:
            maximum = new
            print(maximum)
    
        # custom_imshow(image[0]) 
def check_kitti_on_model():
    args.distributed = False
    config_vit = CONFIGS_ViT_seg[args.vit_name]
    config_vit.n_classes = args.num_classes
    config_vit.n_skip = args.n_skip
    if args.vit_name.find("R50") != -1:
        # config_vit.patches.grid = (int(args.img_size / args.patches_size), int(args.img_size / args.patches_size))
        config_vit.patches.grid = (int(args.img_size_height / args.patches_size), int(args.img_size_width / args.patches_size))
    args.img_size = [args.img_size_height, args.img_size_width]
    
    # dataloader
    dataloader = BtsDataLoader(args, "train")
    dataloader_eval = BtsDataLoader(args, "online_eval")

    
    global_step = 0
    steps_per_epoch = len(dataloader.data)
    num_total_steps = args.num_epochs * steps_per_epoch
    epoch = global_step // steps_per_epoch

    # import model
    model = ViT_seg(config_vit, img_size=args.img_size, num_classes=config_vit.n_classes).cuda()

    # epochs
    for batch_idx, sample_batched in tqdm(dataloader.data): 
        image = torch.autograd.Variable(sample_batched["image"].cuda(args.gpu, non_blocking=True))
        focal = torch.autograd.Variable(sample_batched["focal"].cuda(args.gpu, non_blocking=True))
        depth_gt = torch.autograd.Variable(sample_batched["depth"].cuda(args.gpu, non_blocking=True))

        print("=====================input======================")
        print("image: ", image.shape)
        print("focal:", focal.shape)
        print("depth: ", depth_gt.shape)

        output = model(image).cuda()

        print("=====================output======================")
        print("output shape: ", output.shape)

        # # imshow gt and output
        # custom_imshow(depth_gt[0]) 
        # custom_imshow(output[0].detach()) 

"""


"""        
def train():
    # logging.info(str(args))
    args.distributed = False
    config_vit = CONFIGS_ViT_seg[args.vit_name]
    config_vit.n_classes = args.num_classes
    config_vit.n_skip = args.n_skip
    if args.vit_name.find("R50") != -1:
        # config_vit.patches.grid = (int(args.img_size / args.patches_size), int(args.img_size / args.patches_size))
        config_vit.patches.grid = (int(args.img_size_height / args.patches_size), int(args.img_size_width / args.patches_size))
    args.img_size = [args.img_size_height, args.img_size_width]

    # import model
    model = ViT_seg(config_vit, img_size=args.img_size, num_classes=config_vit.n_classes)
    model.train()
    
    num_params = sum([np.prod(p.size()) for p in model.parameters()])
    print("Total number of parameters: {}".format(num_params))
    num_params_update = sum([np.prod(p.shape) for p in model.parameters() if p.requires_grad])
    print("Total number of learning parameters: {}".format(num_params_update))
    
    model = torch.nn.DataParallel(model)
    model.cuda()

    global_step = 0

    # Training parameters
    base_lr = args.base_lr
    # optimizer = torch.optim.AdamW([{"params": model.module.encoder.parameters(), "weight_decay": args.weight_decay},
    #                                {"params": model.module.decoder.parameters(), "weight_decay": 0}],
    #                               lr=args.learning_rate, eps=args.adam_eps)
    optimizer = optim.SGD(model.parameters(), lr=base_lr, momentum=0.9, weight_decay=0.0001)
    
    # dataloader
    dataloader = BtsDataLoader(args, "train")
    dataloader_eval = BtsDataLoader(args, "online_eval")
    
    # # Logging
    # if not args.multiprocessing_distributed or (args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):
    #     writer = SummaryWriter(args.log_directory + "/" + args.model_name + "/summaries", flush_secs=30)
    #     if args.do_online_eval:
    #         if args.eval_summary_directory != "":
    #             eval_summary_path = os.path.join(args.eval_summary_directory, args.model_name)
    #         else:
    #             eval_summary_path = os.path.join(args.log_directory, "eval")
    #         eval_summary_writer = SummaryWriter(eval_summary_path, flush_secs=30)

    num_classes = args.num_classes
    silog_criterion = silog_loss(variance_focus=args.variance_focus)
    
    start_time = time.time()
    duration = 0

    num_log_images = args.batch_size
    end_learning_rate = args.end_learning_rate if args.end_learning_rate != -1 else 0.1 * args.learning_rate

    var_sum = [var.sum() for var in model.parameters() if var.requires_grad]
    var_cnt = len(var_sum)
    var_sum = np.sum(var_sum)

    print("Initial variables" sum: {:.3f}, avg: {:.3f}".format(var_sum, var_sum/var_cnt))
    
    steps_per_epoch = len(dataloader.data)
    num_total_steps = args.num_epochs * steps_per_epoch
    epoch = global_step // steps_per_epoch

    while epoch < args.num_epochs:
            # if args.distributed:
            #     dataloader.train_sampler.set_epoch(epoch)

            for step, sample_batched in enumerate(dataloader.data):
                optimizer.zero_grad()
                before_op_time = time.time()

                image = torch.autograd.Variable(sample_batched["image"].cuda(args.gpu, non_blocking=True))
                focal = torch.autograd.Variable(sample_batched["focal"].cuda(args.gpu, non_blocking=True))
                depth_gt = torch.autograd.Variable(sample_batched["depth"].cuda(args.gpu, non_blocking=True))

                depth_est = model(image)

                mask = depth_gt > 1.0
            
                loss = silog_criterion.forward(depth_est, depth_gt, mask.to(torch.bool))
                loss.backward()
                for param_group in optimizer.param_groups:
                    current_lr = (args.learning_rate - end_learning_rate) * (1 - global_step / num_total_steps) ** 0.9 + end_learning_rate
                    param_group["lr"] = current_lr

                optimizer.step()

    batch_size = args.batch_size * args.n_gpu

    # writer = SummaryWriter(snapshot_path + "/log")
    iter_num = 0
    max_epoch = args.max_epochs
    max_iterations = args.max_epochs * len(dataloader.data)  # max_epoch = max_iterations // len(trainloader) + 1
    logging.info("{} iterations per epoch. {} max iterations ".format(len(dataloader.data), max_iterations))
    best_performance = 0.0
    iterator = tqdm(range(max_epoch), ncols=70)

    for epoch_num in iterator:
        # epochs
        for sample_batched in tqdm(dataloader.data): 
            image = torch.autograd.Variable(sample_batched["image"].cuda(args.gpu, non_blocking=True))
            focal = torch.autograd.Variable(sample_batched["focal"].cuda(args.gpu, non_blocking=True))
            depth_gt = torch.autograd.Variable(sample_batched["depth"].cuda(args.gpu, non_blocking=True))

            mask = depth_gt > 1.0

            depth_est = model(image)

            loss = silog_criterion.forward(depth_est, depth_gt, mask.to(torch.bool))
            # loss_ce = ce_loss(depth_est, depth_gt[:].long())
            # loss_dice = dice_loss(depth_est, depth_gt, softmax=True)
            # loss = 0.5 * loss_ce + 0.5 * loss_dice
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            lr_ = base_lr * (1.0 - iter_num / max_iterations) ** 0.9
            for param_group in optimizer.param_groups:
                param_group["lr"] = lr_


            iter_num = iter_num + 1
            # writer.add_scalar("info/lr", lr_, iter_num)
            # writer.add_scalar("info/total_loss", loss, iter_num)
            # writer.add_scalar("info/loss_ce", loss_ce, iter_num)

            # logging.info("iteration %d : loss : %f, loss_ce: %f" % (iter_num, loss.item(), loss_ce.item()))
            logging.info("iteration %d : loss : %f, loss_ce: %f" % (iter_num, loss.item(), loss.item()))

            if iter_num % 20 == 0:
                image = image[1, 0:1, :, :]
                image = (image - image.min()) / (image.max() - image.min())
                # writer.add_image("train/Image", image, iter_num)
                depth_est = torch.argmax(torch.softmax(depth_est, dim=1), dim=1, keepdim=True)
                # writer.add_image("train/Prediction", depth_est[1, ...] * 50, iter_num)
                labs = depth_gt[1, ...].unsqueeze(0) * 50
                # writer.add_image("train/GroundTruth", labs, iter_num)

        save_interval = 50  # int(max_epoch/6)
        if epoch_num > int(max_epoch / 2) and (epoch_num + 1) % save_interval == 0:
            # save_mode_path = os.path.join(snapshot_path, "epoch_" + str(epoch_num) + ".pth")
            # torch.save(model.state_dict(), save_mode_path)
            # logging.info("save model to {}".format(save_mode_path))
            logging.info("save model to 1")

        if epoch_num >= max_epoch - 1:
            # save_mode_path = os.path.join(snapshot_path, "epoch_" + str(epoch_num) + ".pth")
            # torch.save(model.state_dict(), save_mode_path)
            # logging.info("save model to {}".format(save_mode_path))
            logging.info("save model to 2")
            iterator.close()
            break

    # writer.close()
    return "Training Finished!"
"""






def online_eval(model, dataloader_eval, gpu, ngpus):
    eval_measures = torch.zeros(10).cuda(device=gpu)
    for _, eval_sample_batched in enumerate(tqdm(dataloader_eval.data)):
        with torch.no_grad():
            image = torch.autograd.Variable(eval_sample_batched["image"].cuda(gpu, non_blocking=True))
            focal = torch.autograd.Variable(eval_sample_batched["focal"].cuda(gpu, non_blocking=True))
            gt_depth = eval_sample_batched["depth"]
            has_valid_depth = eval_sample_batched["has_valid_depth"]
            if not has_valid_depth:
                # print("Invalid depth. continue.")
                continue
            
            # eval일때는 random_crop(352, 704)를 안해줘서 decoder 들어가기 직전 reshape 부분을 [352, 1216] shape으로 바꿔줘야한다.
            depth_est = model(image, reshape_size = [352, 1216])

            depth_est = depth_est.cpu().numpy().squeeze()
            gt_depth = gt_depth.cpu().numpy().squeeze()

        if args.do_kb_crop:
            height, width = gt_depth.shape
            top_margin = int(height - 352)
            left_margin = int((width - 1216) / 2)
            depth_est_uncropped = np.zeros((height, width), dtype=np.float32)
            depth_est_uncropped[top_margin:top_margin + 352, left_margin:left_margin + 1216] = depth_est
            depth_est = depth_est_uncropped

        depth_est[depth_est < args.min_depth_eval] = args.min_depth_eval
        depth_est[depth_est > args.max_depth_eval] = args.max_depth_eval
        depth_est[np.isinf(depth_est)] = args.max_depth_eval
        depth_est[np.isnan(depth_est)] = args.min_depth_eval

        valid_mask = np.logical_and(gt_depth > args.min_depth_eval, gt_depth < args.max_depth_eval)

        if args.garg_crop or args.eigen_crop:
            gt_height, gt_width = gt_depth.shape
            eval_mask = np.zeros(valid_mask.shape)

            if args.garg_crop:
                eval_mask[int(0.40810811 * gt_height):int(0.99189189 * gt_height), int(0.03594771 * gt_width):int(0.96405229 * gt_width)] = 1

            elif args.eigen_crop:
                eval_mask[int(0.3324324 * gt_height):int(0.91351351 * gt_height), int(0.0359477 * gt_width):int(0.96405229 * gt_width)] = 1
                
            valid_mask = np.logical_and(valid_mask, eval_mask)

        measures = compute_errors(gt_depth[valid_mask], depth_est[valid_mask])

        eval_measures[:9] += torch.tensor(measures).cuda(device=gpu)
        eval_measures[9] += 1

    if args.multiprocessing_distributed:
        group = dist.new_group([i for i in range(ngpus)])
        dist.all_reduce(tensor=eval_measures, op=dist.ReduceOp.SUM, group=group)

    if not args.multiprocessing_distributed or gpu == 0:
        eval_measures_cpu = eval_measures.cpu()
        cnt = eval_measures_cpu[9].item()
        eval_measures_cpu /= cnt
        print("Computing errors for {} eval samples".format(int(cnt)))
        print("{:>7}, {:>7}, {:>7}, {:>7}, {:>7}, {:>7}, {:>7}, {:>7}, {:>7}".format("silog", "abs_rel", "log10", "rms",
                                                                                     "sq_rel", "log_rms", "d1", "d2",
                                                                                     "d3"))
        for i in range(8):
            print("{:7.3f}, ".format(eval_measures_cpu[i]), end="")
        print("{:7.3f}".format(eval_measures_cpu[8]))
        return eval_measures_cpu

    return None

def train(gpu, ngpus_per_node, args):
    args.gpu = gpu

    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url, world_size=args.world_size, rank=args.rank)
    
    
    # logging.info(str(args))
    args.distributed = False
    config_vit = CONFIGS_ViT_seg[args.vit_name]
    config_vit.n_classes = args.num_classes
    config_vit.n_skip = args.n_skip
    if args.vit_name.find("R50") != -1:
        # config_vit.patches.grid = (int(args.img_size / args.patches_size), int(args.img_size / args.patches_size))
        config_vit.patches.grid = (int(args.img_size_height / args.patches_size), int(args.img_size_width / args.patches_size))
    
    # for MLP Mixer, the input dimenstion of Mixer must be fixed
    if config_vit.name.find("Mixer") != -1:
        args.img_size = [352, 1216]
    else:
        args.img_size = [args.img_size_height, args.img_size_width]
    # Create model
    model = ViT_seg(config_vit, img_size=args.img_size, num_classes=config_vit.n_classes)
    model.load_from(weights=np.load(config_vit.pretrained_path))
    model.train()
    # # initialize model
    # model.decoder.apply(weights_init_xavier)
    # model.segmentation_head.apply(weights_init_xavier)
    # set_misc(model)

    num_params = sum([np.prod(p.size()) for p in model.parameters()])
    print("Total number of parameters: {}".format(num_params))

    num_params_update = sum([np.prod(p.shape) for p in model.parameters() if p.requires_grad])
    print("Total number of learning parameters: {}".format(num_params_update))

    if args.distributed:
        if args.gpu is not None:
            torch.cuda.set_device(args.gpu)
            model.cuda(args.gpu)
            args.batch_size = int(args.batch_size / ngpus_per_node)
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True)
        else:
            model.cuda()
            model = torch.nn.parallel.DistributedDataParallel(model, find_unused_parameters=True)
    else:
        model = torch.nn.DataParallel(model)
        model.cuda()

    if args.distributed:
        print("Model Initialized on GPU: {}".format(args.gpu))
    else:
        print("Model Initialized")

    global_step = 0
    best_eval_measures_lower_better = torch.zeros(6).cpu() + 1e3
    best_eval_measures_higher_better = torch.zeros(3).cpu()
    best_eval_steps = np.zeros(9, dtype=np.int32)

    # Training parameters
    base_lr = args.base_lr
    # optimizer = torch.optim.AdamW([{"params": model.module.encoder.parameters(), "weight_decay": args.weight_decay},
    #                                {"params": model.module.decoder.parameters(), "weight_decay": 0}],
    #                               lr=args.learning_rate, eps=args.adam_eps)
    optimizer = optim.SGD(model.parameters(), lr=base_lr, momentum=0.9, weight_decay=0.0001)

    model_just_loaded = False
    if args.checkpoint_path != "":
        if os.path.isfile(args.checkpoint_path):
            print("Loading checkpoint '{}'".format(args.checkpoint_path))
            if args.gpu is None:
                checkpoint = torch.load(args.checkpoint_path)
            else:
                loc = "cuda:{}".format(args.gpu)
                checkpoint = torch.load(args.checkpoint_path, map_location=loc)
            global_step = checkpoint["global_step"]
            model.load_state_dict(checkpoint["model"])
            optimizer.load_state_dict(checkpoint["optimizer"])
            try:
                best_eval_measures_higher_better = checkpoint["best_eval_measures_higher_better"].cpu()
                best_eval_measures_lower_better = checkpoint["best_eval_measures_lower_better"].cpu()
                best_eval_steps = checkpoint["best_eval_steps"]
            except KeyError:
                print("Could not load values for online evaluation")

            print("Loaded checkpoint '{}' (global_step {})".format(args.checkpoint_path, checkpoint["global_step"]))
        else:
            print("No checkpoint found at '{}'".format(args.checkpoint_path))
        model_just_loaded = True

    if args.retrain:
        global_step = 0

    cudnn.benchmark = True

    if config_vit.name.find("Mixer") != -1:
        args.do_random_crop = False

    dataloader = BtsDataLoader(args, "train")
    dataloader_eval = BtsDataLoader(args, "online_eval")

    # Logging
    if not args.multiprocessing_distributed or (args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):
        writer = SummaryWriter(args.log_directory + "/" + args.model_name + "/summaries", flush_secs=30)
        if args.do_online_eval:
            if args.eval_summary_directory != "":
                eval_summary_path = os.path.join(args.eval_summary_directory, args.model_name)
            else:
                eval_summary_path = os.path.join(args.log_directory, "eval")
            eval_summary_writer = SummaryWriter(eval_summary_path, flush_secs=30)


    # # loss function
    silog_criterion = silog_loss(variance_focus=args.variance_focus)
    # ce_loss = nn.CrossEntropyLoss()
    # dice_loss = DiceLoss(num_classes)

    start_time = time.time()
    duration = 0

    num_log_images = args.batch_size
    end_learning_rate = args.end_learning_rate if args.end_learning_rate != -1 else 0.1 * args.learning_rate

    var_sum = [var.sum() for var in model.parameters() if var.requires_grad]
    var_cnt = len(var_sum)
    var_sum = torch.FloatTensor(var_sum)
    var_sum = torch.sum(var_sum)
    # var_sum = np.sum(var_sum)

    print("Initial variables sum: {:.3f}, avg: {:.3f}".format(var_sum, var_sum/var_cnt))

    steps_per_epoch = len(dataloader.data)
    num_total_steps = args.num_epochs * steps_per_epoch
    epoch = global_step // steps_per_epoch

    loss_list, valloss_list = [], []

    while epoch < args.num_epochs:
        if args.distributed:
            dataloader.train_sampler.set_epoch(epoch)

        for step, sample_batched in enumerate(dataloader.data):
            optimizer.zero_grad()
            before_op_time = time.time()

            image = torch.autograd.Variable(sample_batched["image"].cuda(args.gpu, non_blocking=True))
            focal = torch.autograd.Variable(sample_batched["focal"].cuda(args.gpu, non_blocking=True))
            depth_gt = torch.autograd.Variable(sample_batched["depth"].cuda(args.gpu, non_blocking=True))
            
            
            depth_est = model(image, reshape_size = args.img_size)
 
            mask = depth_gt > 1.0

            # loss_ce = ce_loss(depth_est[mask], depth_gt[mask])
            # loss_dice = dice_loss(depth_est[mask], depth_gt[mask], softmax=True)
            # loss = 0.5 * loss_ce + 0.5 * loss_dice
            loss = silog_criterion.forward(depth_est, depth_gt, mask.to(torch.bool))

            loss.backward()
            for param_group in optimizer.param_groups:
                current_lr = (args.learning_rate - end_learning_rate) * (1 - global_step / num_total_steps) ** 0.9 + end_learning_rate
                param_group["lr"] = current_lr

            optimizer.step()

            if not args.multiprocessing_distributed or (args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):
                print("[epoch][s/s_per_e/gs]: [{}][{}/{}/{}], lr: {:.12f}, loss: {:.12f}".format(epoch, step, steps_per_epoch, global_step, current_lr, loss))
                if np.isnan(loss.cpu().item()):
                    print("NaN in loss occurred. Aborting training.")
                    return -1

            duration += time.time() - before_op_time
            if global_step and global_step % args.log_freq == 0 and not model_just_loaded:
                var_sum = [var.sum() for var in model.parameters() if var.requires_grad]
                var_cnt = len(var_sum)
                var_sum = torch.FloatTensor(var_sum)
                var_sum = torch.sum(var_sum)
                # var_sum = np.sum(var_sum)
                examples_per_sec = args.batch_size / duration * args.log_freq
                duration = 0
                time_sofar = (time.time() - start_time) / 3600
                training_time_left = (num_total_steps / global_step - 1.0) * time_sofar
                if not args.multiprocessing_distributed or (args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):
                    print("{}".format(args.model_name))
                print_string = "GPU: {} | examples/s: {:4.2f} | loss: {:.5f} | var sum: {:.3f} avg: {:.3f} | time elapsed: {:.2f}h | time left: {:.2f}h"
                print(print_string.format(args.gpu, examples_per_sec, loss, var_sum.item(), var_sum.item()/var_cnt, time_sofar, training_time_left))

                if not args.multiprocessing_distributed or (args.multiprocessing_distributed
                                                            and args.rank % ngpus_per_node == 0):
                    writer.add_scalar("silog_loss", loss, global_step)
                    writer.add_scalar("learning_rate", current_lr, global_step)
                    writer.add_scalar("var average", var_sum.item()/var_cnt, global_step)
                    depth_gt = torch.where(depth_gt < 1e-3, depth_gt * 0 + 1e3, depth_gt)
                    for i in range(num_log_images):
                        writer.add_image("depth_gt/image/{}".format(i), normalize_result(1/depth_gt[i, :, :, :].data), global_step)
                        writer.add_image("depth_est/image/{}".format(i), normalize_result(1/depth_est[i, :, :, :].data), global_step)
                        # writer.add_image("reduc1x1/image/{}".format(i), normalize_result(1/reduc1x1[i, :, :, :].data), global_step)
                        # writer.add_image("lpg2x2/image/{}".format(i), normalize_result(1/lpg2x2[i, :, :, :].data), global_step)
                        # writer.add_image("lpg4x4/image/{}".format(i), normalize_result(1/lpg4x4[i, :, :, :].data), global_step)
                        # writer.add_image("lpg8x8/image/{}".format(i), normalize_result(1/lpg8x8[i, :, :, :].data), global_step)
                        writer.add_image("image/image/{}".format(i), inv_normalize(image[i, :, :, :]).data, global_step)
                    writer.flush()

            if not args.do_online_eval and global_step and global_step % args.save_freq == 0:
                if not args.multiprocessing_distributed or (args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):
                    checkpoint = {"global_step": global_step,
                                  "model": model.state_dict(),
                                  "optimizer": optimizer.state_dict()}
                    torch.save(checkpoint, args.log_directory + "/" + args.model_name + "/model-{}".format(global_step))


            if args.do_online_eval and global_step and global_step % args.eval_freq == 0 and not model_just_loaded:
                time.sleep(0.1)
                model.eval()
                eval_measures = online_eval(model, dataloader_eval, gpu, ngpus_per_node)
                loss_list.append(loss.item())
                valloss_list.append(eval_measures[:9].tolist())
                plotgraph(loss_list, valloss_list, path = args.log_directory + "/" + args.model_name, description="")
                if eval_measures is not None:
                    for i in range(9):
                        eval_summary_writer.add_scalar(eval_metrics[i], eval_measures[i].cpu(), int(global_step))
                        measure = eval_measures[i]
                        is_best = False
                        if i < 6 and measure < best_eval_measures_lower_better[i]:
                            old_best = best_eval_measures_lower_better[i].item()
                            best_eval_measures_lower_better[i] = measure.item()
                            is_best = True
                        elif i >= 6 and measure > best_eval_measures_higher_better[i-6]:
                            old_best = best_eval_measures_higher_better[i-6].item()
                            best_eval_measures_higher_better[i-6] = measure.item()
                            is_best = True
                        if is_best:
                            old_best_step = best_eval_steps[i]
                            old_best_name = "/model-{}-best_{}_{:.5f}".format(old_best_step, eval_metrics[i], old_best)
                            model_path = args.log_directory + "/" + args.model_name + old_best_name
                            if os.path.exists(model_path):
                                # # linux
                                # command = "rm {}".format(model_path)
                                # os.system(command)
                                # window
                                os.remove(model_path)
                            best_eval_steps[i] = global_step
                            model_save_name = "/model-{}-best_{}_{:.5f}".format(global_step, eval_metrics[i], measure)
                            print("New best for {}. Saving model: {}".format(eval_metrics[i], model_save_name))
                            checkpoint = {"global_step": global_step,
                                          "model": model.state_dict(),
                                          "optimizer": optimizer.state_dict(),
                                          "best_eval_measures_higher_better": best_eval_measures_higher_better,
                                          "best_eval_measures_lower_better": best_eval_measures_lower_better,
                                          "best_eval_steps": best_eval_steps
                                          }
                            torch.save(checkpoint, args.log_directory + "/" + args.model_name + model_save_name)
                    eval_summary_writer.flush()
                model.train()
                block_print()
                # set_misc(model)
                enable_print()

            model_just_loaded = False
            global_step += 1

        epoch += 1



def main():
    if args.mode != "train":
        print("bts_main.py is only for training. Use bts_test.py instead.")
        return -1

    model_filename = args.model_name + ".py"
    # # Linux
    # command = "mkdir " + args.log_directory + "/" + args.model_name
    # os.system(command)
    # Windows
    if not os.path.isdir(args.log_directory + "/" + args.model_name):
        os.mkdir(args.log_directory + "/" + args.model_name)

    import shutil
    args_out_path = args.log_directory + "/" + args.model_name + "/" + sys.argv[1]
    # Linux
    # command = "copy " + sys.argv[1] + " " + args_out_path
    # os.system(command)
    # Windows
    shutil.copyfile(sys.argv[1], args_out_path)

    if args.checkpoint_path == "":
        model_out_path = args.log_directory + "/" + args.model_name + "/" + model_filename
        # # Linux
        # command = "copy bts.py " + model_out_path
        # os.system(command)
        # Windows
        shutil.copy2("models/model.py", model_out_path)
        aux_out_path = args.log_directory + "/" + args.model_name + "/."
        # # Linux
        # command = "copy bts_main.py " + aux_out_path
        # os.system(command)
        # Windows
        shutil.copy2("main.py", aux_out_path)
        # # Linux
        # command = "copy bts_dataloader.py " + aux_out_path
        # os.system(command)
        # Windows
        shutil.copy2("dataloader.py", aux_out_path)
    else:
        loaded_model_dir = os.path.dirname(args.checkpoint_path)
        loaded_model_name = os.path.basename(loaded_model_dir)
        loaded_model_filename = loaded_model_name + ".py"

        model_out_path = args.log_directory + "/" + args.model_name + "/" + model_filename
        # # Linux
        # command = "copy " + loaded_model_dir + "/" + loaded_model_filename + " " + model_out_path
        # os.system(command)
        # Windows
        shutil.copy2(loaded_model_dir + "/" + loaded_model_filename, model_out_path)

    torch.cuda.empty_cache()
    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    ngpus_per_node = torch.cuda.device_count()
    if ngpus_per_node > 1 and not args.multiprocessing_distributed:
        print("This machine has more than 1 gpu. Please specify --multiprocessing_distributed, or set \"CUDA_VISIBLE_DEVICES=0\"")
        return -1

    if args.do_online_eval:
        print("You have specified --do_online_eval.")
        print("This will evaluate the model every eval_freq {} steps and save best models for individual eval metrics."
              .format(args.eval_freq))

    if args.multiprocessing_distributed:
        args.world_size = ngpus_per_node * args.world_size
        mp.spawn(train, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
    else:
        train(args.gpu, ngpus_per_node, args)

if __name__ == "__main__":
    # # check dataloader
    # check_dataloader()
    
    # # check model
    # kitti = torch.randn(1, 3, 352, 704).cuda()
    
    # config_vit = CONFIGS_ViT_seg[args.vit_name]
    # config_vit.n_classes = args.num_classes
    # config_vit.n_skip = args.n_skip
    # if args.vit_name.find("R50") != -1:
    #     # config_vit.patches.grid = (int(args.img_size / args.patches_size), int(args.img_size / args.patches_size))
    #     config_vit.patches.grid = (int(args.img_size_height / args.patches_size), int(args.img_size_width / args.patches_size))
    # args.img_size = [args.img_size_height, args.img_size_width]
    # model = ViT_seg(config_vit, img_size=args.img_size, num_classes=config_vit.n_classes).cuda()

    # output = model(kitti)
    # print("output of TransUnet shape:", output.shape)

    # # check model with kitti dataloader
    # check_kitti_on_model()

    # # train
    main()