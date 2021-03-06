import argparse
import glob
import json
import multiprocessing
import os
import random
import re
from importlib import import_module
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
from torch.optim.lr_scheduler import StepLR, CosineAnnealingLR, CosineAnnealingWarmRestarts
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from dataset import MaskBaseDataset
from loss import create_criterion

import nni
from nni.utils import merge_parameter
from optims import SGD_GC, SGDW, SGDW_GC, Adam_GC, AdamW, AdamW_GC
from apex import amp

def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def grid_image(np_images, gts, preds, n=16, shuffle=False):
    batch_size = np_images.shape[0]
    assert n <= batch_size

    choices = random.choices(range(batch_size), k=n) if shuffle else list(range(n))
    figure = plt.figure(figsize=(12, 18 + 2))  # cautions: hardcoded, 이미지 크기에 따라 figsize 를 조정해야 할 수 있습니다. T.T
    plt.subplots_adjust(top=0.8)               # cautions: hardcoded, 이미지 크기에 따라 top 를 조정해야 할 수 있습니다. T.T
    n_grid = np.ceil(n ** 0.5)
    tasks = ["mask", "gender", "age"]
    for idx, choice in enumerate(choices):
        gt = gts[choice].item()
        pred = preds[choice].item()
        image = np_images[choice]
        # title = f"gt: {gt}, pred: {pred}"
        gt_decoded_labels = MaskBaseDataset.decode_multi_class(gt)
        pred_decoded_labels = MaskBaseDataset.decode_multi_class(pred)
        title = "\n".join([
            f"{task} - gt: {gt_label}, pred: {pred_label}"
            for gt_label, pred_label, task
            in zip(gt_decoded_labels, pred_decoded_labels, tasks)
        ])

        plt.subplot(n_grid, n_grid, idx + 1, title=title)
        plt.xticks([])
        plt.yticks([])
        plt.grid(False)
        plt.imshow(image, cmap=plt.cm.binary)

    return figure


def increment_path(path, exist_ok=False):
    """ Automatically increment path, i.e. runs/exp --> runs/exp0, runs/exp1 etc.

    Args:
        path (str or pathlib.Path): f"{model_dir}/{args.name}".
        exist_ok (bool): whether increment path (increment if False).
    """
    path = Path(path)
    if (path.exists() and exist_ok) or (not path.exists()):
        return str(path)
    else:
        dirs = glob.glob(f"{path}*")
        matches = [re.search(rf"%s(\d+)" % path.stem, d) for d in dirs]
        i = [int(m.groups()[0]) for m in matches if m]
        n = max(i) + 1 if i else 2
        return f"{path}{n}"


def train(data_dir, model_dir, args):
    seed_everything(args.seed)

    save_dir = increment_path(os.path.join(model_dir, args.name))

    # -- settings
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    # -- dataset
    dataset_module = getattr(import_module("dataset"), args.dataset)  # default: MaskBaseDataset
    dataset = dataset_module(
        data_dir=data_dir,
    )
    num_classes = dataset.num_classes  # 18

    # -- augmentation
    transform_module = getattr(import_module("dataset"), args.augmentation)  # default: BaseAugmentation
    transform = transform_module(
        resize=args.resize,
        mean=dataset.mean,
        std=dataset.std,
    )
    # val_loader는 augmentation하지 않음
    val_transform_module = getattr(import_module("dataset"), "BaseAugmentation")  # default: BaseAugmentation
    val_transform = val_transform_module(
        resize=args.resize,
        mean=dataset.mean,
        std=dataset.std,
    )

    # -- data_loader
    train_set, val_set = dataset.split_dataset()

    train_loader = DataLoader(
        train_set,
        batch_size=args.batch_size,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=True,
        pin_memory=use_cuda,
        drop_last=True,
    )

    val_loader = DataLoader(
        val_set,
        batch_size=args.valid_batch_size,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=True,
        pin_memory=use_cuda,
        drop_last=False,
    )

    """test_loader = DataLoader(
        test_set,
        batch_size=64,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=False,
        pin_memory=use_cuda,
        drop_last=True,
    )"""
    
    # -- model
    model_module = getattr(import_module("model"), args.model)  # default: BaseModel
    model = model_module(
        num_classes=num_classes
    ).to(device)
    # 저장된 모델 불러와서 학습
    if args.reuse_param_exp != "None":
        model_path = os.path.join(args.model_dir, args.reuse_param_exp, 'best.pth')
        model.load_state_dict(torch.load(model_path, map_location=device))
    

    # pretrain된 데이터와 높은 유사성을 가질 때 사용하는 parameter freeze
    if (args.unfreeze_param != "None") or (args.freeze == True):
        for name, para in model.named_parameters():
            if args.unfreeze_param in name:
                para.requires_grad = True
            else:
                para.requires_grad = False



    # -- loss & metric
    if args.p != 0:
        criterion = create_criterion(args.criterion, P=args.p)
    else:
        criterion = create_criterion(args.criterion)


    if args.optimizer == "Adam_GC":
        opt_module = getattr(import_module("optims"), args.optimizer)
    else:
        opt_module = getattr(import_module("torch.optim"), args.optimizer)  # default: SGD
    
    optimizer = opt_module(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=args.lr,
        weight_decay=5e-5
    )
    model, optimizer = amp.initialize(model, optimizer, opt_level="O1") # amp
    model = torch.nn.DataParallel(model)
    # scheduler = StepLR(optimizer, args.lr_decay_step, gamma=0.5)
    scheduler = CosineAnnealingLR(optimizer, T_max=10, eta_min=1e-6, last_epoch=-1)

    # -- logging
    logger = SummaryWriter(log_dir=save_dir)
    with open(os.path.join(save_dir, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(vars(args), f, ensure_ascii=False, indent=4)

    best_val_acc = 0
    best_f1_score = 0
    best_val_loss = np.inf
    for epoch in range(args.epochs):
        # train loop
        model.train()
        loss_value = 0
        matches = 0
        dataset.set_transform(transform) # dataset transform (augmentation)
        for idx, train_batch in enumerate(train_loader):
            inputs, labels = train_batch
            inputs = inputs.to(device)
            labels = labels.to(device)

            # mix_up
            if args.mix_up:
                lam = np.random.beta(1.0, 1.0)
                index = torch.randperm(args.batch_size).to(device)
                inputs = lam * inputs + (1 - lam) * inputs[index]
                labels_a, labels_b = labels, labels[index]

            optimizer.zero_grad()

            outs = model(inputs)
            preds = torch.argmax(outs, dim=-1)
            if args.mix_up:
                def MixUp(criterion, outs, labels_a, labels_b, lam):
                    return lam * criterion(outs, labels_a) + (1 - lam) * criterion(outs, labels_b)
                loss = MixUp(criterion, outs, labels_a, labels_b, lam)
            else:
                loss = criterion(outs, labels)

            # amp_scaled_loss
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
            #loss.backward()
            optimizer.step()

            loss_value += loss.item()
            matches += (preds == labels).sum().item()
            if (idx + 1) % args.log_interval == 0:
                train_loss = loss_value / args.log_interval
                train_acc = matches / args.batch_size / args.log_interval
                current_lr = get_lr(optimizer)
                print(
                    f"Epoch[{epoch}/{args.epochs}]({idx + 1}/{len(train_loader)}) || "
                    f"training loss {train_loss:4.4} || training accuracy {train_acc:4.2%} || lr {current_lr}"
                )
                logger.add_scalar("Train/loss", train_loss, epoch * len(train_loader) + idx)
                logger.add_scalar("Train/accuracy", train_acc, epoch * len(train_loader) + idx)

                loss_value = 0
                matches = 0

        scheduler.step()

        # val loop
        with torch.no_grad():
            print("Calculating validation results...")
            model.eval()
            val_loss_items = []
            val_acc_items = []
            f1_items = []
            test_acc_items = []
            test_f1_items = []
            figure = None
            dataset.set_transform(val_transform) # val dataset은 augmentation 하지 않음
            for val_batch in val_loader:
                inputs, labels = val_batch
                inputs = inputs.to(device)
                labels = labels.to(device)

                outs = model(inputs)
                preds = torch.argmax(outs, dim=-1)

                loss_item = criterion(outs, labels).item()
                # F1 score
                F1 = create_criterion("f1")
                _f1_item = F1(outs, labels).item()
                f1_item = 1 - _f1_item

                acc_item = (labels == preds).sum().item()
                val_loss_items.append(loss_item)
                val_acc_items.append(acc_item)
                f1_items.append(f1_item)

                if figure is None:
                    inputs_np = torch.clone(inputs).detach().cpu().permute(0, 2, 3, 1).numpy()
                    inputs_np = dataset_module.denormalize_image(inputs_np, dataset.mean, dataset.std)
                    figure = grid_image(
                        inputs_np, labels, preds, n=16, shuffle=args.dataset != "MaskSplitByProfileDataset"
                    )

            val_loss = np.sum(val_loss_items) / len(val_loader)
            val_acc = np.sum(val_acc_items) / len(val_set)
            f1_score = np.sum(f1_items) / len(val_loader)

            best_val_loss = min(best_val_loss, val_loss)
            if not args.f1_acc:
                if val_acc > best_val_acc:
                    print(f"New best model for val accuracy : {val_acc:4.2%}! saving the best model..")
                    torch.save(model.module.state_dict(), f"{save_dir}/best.pth")
                    best_val_acc = val_acc
                    best_f1_score = f1_score
            elif args.f1_acc:
                if f1_score > best_f1_score:
                    print(f"New best model for f1 score : {f1_score:4.2%}! saving the best model..")
                    torch.save(model.module.state_dict(), f"{save_dir}/best.pth")
                    best_val_acc = val_acc
                    best_f1_score = f1_score
            
            wrong = 0
            age = 0
            sex = 0
            age_sex = 0
            mask = 0
            all = 0

            """for test_batch in test_loader:
                inputs, labels = test_batch
                inputs = inputs.to(device)
                labels = labels.to(device)

                outs = model(inputs)
                preds_list = torch.sort(outs, dim=-1, )
                preds = torch.argmax(outs, dim=-1)
                
                for i in range(64):
                    all += 1
                    if preds[i] != labels[i]:
                        num = abs(preds[i] - labels[i])
                        if num == 1:
                            age += 1
                        elif num == 3:
                            sex += 1
                        elif num == 4:
                            age_sex += 1
                        elif num == 6:
                            mask += 1
                        wrong += 1            

                # F1 score
                F1 = create_criterion("f1")
                _f1_item = F1(outs, labels).item()
                f1_item = 1 - _f1_item

                acc_item = (labels == preds).sum().item()
                #test_acc_items.append(acc_item)
                #test_f1_items.append(f1_item)
            """
            
            """print(f"전체 : {all}, 틀린갯수 : {wrong}, 나이 : {age}, 성별 : {sex}, 성별과 나이 : {age_sex}, 마스크 : {mask}")
            test_acc = np.sum(test_acc_items) / len(test_set)
            test_f1_score = np.sum(test_f1_items) / len(test_loader)"""


            torch.save(model.module.state_dict(), f"{save_dir}/last.pth")
            print(
                f"[Val] acc : {val_acc:4.2%}, loss: {val_loss:4.2}, f1_score: {f1_score:4.2%} || "
                f"best acc : {best_val_acc:4.2%}, best loss: {best_val_loss:4.2}, best f1_score : {best_f1_score:4.2%}"
            )
            #print(f"----------[Test] acc : {test_acc:4.2%}, f1_score: {test_f1_score:4.2%}")
            logger.add_scalar("Val/loss", val_loss, epoch)
            logger.add_scalar("Val/accuracy", val_acc, epoch)
            logger.add_scalar("Val/f1_score", f1_score, epoch)
            logger.add_figure("results", figure, epoch)
            print()
        nni.report_intermediate_result(f1_score)
    nni.report_final_result(f1_score)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    from dotenv import load_dotenv
    import os
    load_dotenv(verbose=True)

    # Data and model checkpoints directories
    parser.add_argument('--seed', type=int, default=0, help='random seed (default: 42)')
    parser.add_argument('--epochs', type=int, default=20, help='number of epochs to train (default: 20)')
    parser.add_argument('--dataset', type=str, default='MaskBaseDataset', help='dataset augmentation type (default: MaskSplitByProfileDataset)')
    parser.add_argument('--augmentation', type=str, default='CustomAugmentation', help='data augmentation type (default: BaseAugmentation)')
    parser.add_argument("--resize", nargs="+", type=int, default=[224, 224], help='resize size for image when training')
    parser.add_argument('--batch_size', type=int, default=64, help='input batch size for training (default: 64)')
    parser.add_argument('--valid_batch_size', type=int, default=64, help='input batch size for validing (default: 1000)')
    parser.add_argument('--model', type=str, default='Preresnet18', help='model type (default: BaseModel)')
    parser.add_argument('--optimizer', type=str, default='Adam', help='optimizer type (default: Adam)')
    parser.add_argument('--lr', type=float, default=1e-4, help='learning rate (default: 1e-3)')
    parser.add_argument('--val_ratio', type=float, default=0.2, help='ratio for validaton (default: 0.2)')
    parser.add_argument('--criterion', type=str, default='focal', help='criterion type (default: focal)')
    parser.add_argument('--lr_decay_step', type=int, default=20, help='learning rate scheduler deacy step (default: 20)')
    parser.add_argument('--lr_scheduler', type=str, default='CosineAnnealingLR', help='learning rate scheduler')
    parser.add_argument('--log_interval', type=int, default=20, help='how many batches to wait before logging training status')
    parser.add_argument('--name', default='exp', help='model save at {SM_MODEL_DIR}/{name}')

    # joon's args
    parser.add_argument('--p', type=float, default=0.0, help='criterion sum ratio')
    parser.add_argument('--reuse_param_exp', default='None', help='reuse parameters in ./model/exp_name')
    parser.add_argument('--freeze', default='False', help='freeze backbone')
    parser.add_argument('--unfreeze_param', type=str, default='None', help='unfreeze parameters')
    parser.add_argument('--mix_up', default=True, help='mix_up')
    parser.add_argument('--f1_acc', default=True, help='f1_acc')

    # Container environment
    parser.add_argument('--data_dir', type=str, default=os.environ.get('SM_CHANNEL_TRAIN', '/opt/ml/input/data/train/images'))
    parser.add_argument('--model_dir', type=str, default=os.environ.get('SM_MODEL_DIR', './model'))

    args = parser.parse_args()
    tuner_params = nni.get_next_parameter()
    args = merge_parameter(args, tuner_params)

    print(args)

    data_dir = args.data_dir
    model_dir = args.model_dir

    train(data_dir, model_dir, args)
