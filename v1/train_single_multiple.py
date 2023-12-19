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
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from dataset import MaskBaseDataset
from loss import create_criterion

from accuracy_loss_print import AccuracyLoss


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
        return param_group["lr"]


def grid_image(np_images, gts, preds, n=16, shuffle=False):
    batch_size = np_images.shape[0]
    assert n <= batch_size

    choices = random.choices(range(batch_size), k=n) if shuffle else list(range(n))
    figure = plt.figure(
        figsize=(12, 18 + 2)
    )  # cautions: hardcoded, 이미지 크기에 따라 figsize 를 조정해야 할 수 있습니다. T.T
    plt.subplots_adjust(
        top=0.8
    )  # cautions: hardcoded, 이미지 크기에 따라 top 를 조정해야 할 수 있습니다. T.T
    n_grid = int(np.ceil(n**0.5))
    tasks = ["mask", "gender", "age"]
    for idx, choice in enumerate(choices):
        gt = gts[choice].item()
        pred = preds[choice].item()
        image = np_images[choice]
        gt_decoded_labels = MaskBaseDataset.decode_multi_class(gt)
        pred_decoded_labels = MaskBaseDataset.decode_multi_class(pred)
        title = "\n".join(
            [
                f"{task} - gt: {gt_label}, pred: {pred_label}"
                for gt_label, pred_label, task in zip(
                    gt_decoded_labels, pred_decoded_labels, tasks
                )
            ]
        )

        plt.subplot(n_grid, n_grid, idx + 1, title=title)
        plt.xticks([])
        plt.yticks([])
        plt.grid(False)
        plt.imshow(image, cmap=plt.cm.binary)

    return figure


def increment_path(path, exist_ok=False):
    """Automatically increment path, i.e. runs/exp --> runs/exp0, runs/exp1 etc.

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
    dataset_module = getattr(
        import_module("dataset"), args.dataset
    )  # default: MaskBaseDataset
    dataset = dataset_module(
        data_dir=data_dir,
    )
    num_classes = dataset.num_classes  # 18

    # -- augmentation
    transform_module = getattr(
        import_module("dataset"), args.augmentation
    )  # default: BaseAugmentation
    transform = transform_module(
        resize=args.resize,
        mean=dataset.mean,
        std=dataset.std,
    )
    dataset.set_transform(transform)

    # -- data_loader
    train_set, val_set = dataset.split_dataset()

    train_loader = DataLoader(
        train_set,
        batch_size=args.batch_size,
        num_workers=multiprocessing.cpu_count() // 2,
        shuffle=True,
        pin_memory=use_cuda,
        drop_last=True,
    )

    val_loader = DataLoader(
        val_set,
        batch_size=args.valid_batch_size,
        num_workers=multiprocessing.cpu_count() // 2,
        shuffle=False,
        pin_memory=use_cuda,
        drop_last=True,
    )

    # -- model
    model_module = getattr(import_module("model"), args.model)  # default: BaseModel
    model_age = model_module(num_classes=1).to(device)
    model_age = torch.nn.DataParallel(model_age)
    model_mask_gender = model_module(num_classes=6).to(device)
    model_maks_gender = torch.nn.DataParallel(model_mask_gender)
        
    # -- loss & metric
    criterion_age = create_criterion(args.criterion_age)  # default: f1
    criterion_mask_gender = create_criterion(args.criterion_mask_gender)
    
    opt_module = getattr(import_module("torch.optim"), args.optimizer)  # default: SGD
    optimizer_age = opt_module(
        filter(lambda p: p.requires_grad, model_age.parameters()),
        lr=args.lr,
        weight_decay=5e-4,
    )
    optimizer_mask_gender = opt_module(
        filter(lambda p: p.requires_grad, model_mask_gender.parameters()),
        lr=args.lr,
        weight_decay=5e-4,
    )
        
    
    scheduler_age = StepLR(optimizer_age, args.lr_decay_step, gamma=0.5)
    scheduler_mask_gender = StepLR(optimizer_mask_gender, args.lr_decay_step, gamma=0.5)

    # -- logging
    logger = SummaryWriter(log_dir=save_dir)
    with open(os.path.join(save_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(vars(args), f, ensure_ascii=False, indent=4)

    best_val_acc = 0
    best_val_loss = np.inf
    best_epoch = 0 
    
    best_val_age_acc = 0
    best_epoch_age = 0
    
    start_epoch = 0
    
    # if args.resume_from:
    #     model_data = torch.load(args.resume_from)
    #     model.load_state_dict(model_data['model_state_dict'])
    #     optimizer.load_state_dict(model_data['optimizer_state_dict'])
    #     start_epoch = model_data['epoch'] + 1
    
    torch.autograd.set_detect_anomaly(True)
    for epoch in range(start_epoch, args.epochs):
        torch.cuda.empty_cache()
        # train loop
        model_age.train()
        model_mask_gender.train()
        loss_val_age = 0
        loss_val_mask_gender = 0
        matches = 0
        value_mapping = {0:0.0, 1: 45.0, 2: 100.0}  
        for idx, train_batch in enumerate(train_loader):
            inputs, labels = train_batch
            mask_label,gender_label, age_label = MaskBaseDataset.decode_multi_class(labels)
            
            
            age_value = torch.tensor([value_mapping[val] for val in age_label.tolist()])
            mask_gender_label=MaskBaseDataset.encode_mask_gender(mask_label,gender_label)
            
            inputs = inputs.to(device)
            age_value,mask_gender_label = torch.tensor(age_value).to(device),torch.tensor(mask_gender_label).to(device)

            optimizer_age.zero_grad()
            optimizer_mask_gender.zero_grad()


            outs_age = model_age(inputs)
            outs_mask_gender = model_mask_gender(inputs)
            preds_mask_gender = torch.argmax(outs_mask_gender, dim=-1)
            outs_age_list=[]
            for item in outs_age:
                if(item<30):
                    outs_age_list.append(0.0)
                elif(item<60):
                    outs_age_list.append(45.0)
                else:
                    outs_age_list.append(100.0)
            preds_age=torch.tensor(outs_age_list).to(device)
                

            loss_age = criterion_age(outs_age, age_value)
            loss_mask_gender = criterion_mask_gender(outs_mask_gender, mask_gender_label)
            
            loss_age.backward()
            optimizer_age.step()
            
            loss_mask_gender.backward()
            optimizer_mask_gender.step()
            

            
            
            
            loss_val_age += loss_age.item()
            loss_val_mask_gender += loss_mask_gender.item()
            correct_predictions = (preds_age == age_value) & (preds_mask_gender == mask_gender_label) 
            matches += correct_predictions.sum().item()
            if (idx + 1) % args.log_interval == 0:
                train_loss_age = loss_val_age / args.log_interval
                train_loss_mask_gender = loss_val_mask_gender / args.log_interval
                train_acc = matches / args.batch_size / args.log_interval
                current_lr_age = get_lr(optimizer_age)
                current_lr_gender = get_lr(optimizer_mask_gender)
                

                print(
                    f"Epoch[{epoch}/{args.epochs}]({idx + 1}/{len(train_loader)}) || "
                    f"training age loss {train_loss_age:4.4} ||training mask/gender loss {train_loss_mask_gender:4.4} || training accuracy {train_acc:4.2%} || \n"
                )
                logger.add_scalar(
                    "Train/loss", train_loss_age, epoch * len(train_loader) + idx
                )
                logger.add_scalar(
                    "Train/accuracy", train_acc, epoch * len(train_loader) + idx
                )

                loss_val_age =0
                loss_val_mask_gender=0
                matches = 0

        scheduler_age.step()
        scheduler_mask_gender.step()

        # val loop
        with torch.no_grad():
            print("Calculating validation results...")
            model_age.eval()
            model_mask_gender.eval()
            val_loss_items_age = []
            val_loss_items_mask_gender = []
            val_acc_items = []
            val_acc_items_age = []
            figure = None
            for val_batch in val_loader:
                inputs, labels = val_batch
                mask_label,gender_label, age_label = MaskBaseDataset.decode_multi_class(labels)
                
                age_value = torch.tensor([value_mapping[val] for val in age_label.tolist()])
                mask_gender_label=MaskBaseDataset.encode_mask_gender(mask_label,gender_label)
                
                inputs = inputs.to(device)
                age_value,mask_gender_label = torch.tensor(age_value).to(device),torch.tensor(mask_gender_label).to(device)

                outs_age = model_age(inputs)
                outs_mask_gender = model_mask_gender(inputs)
                preds_mask_gender = torch.argmax(outs_mask_gender, dim=-1)
                outs_age_list=[]
                for item in outs_age:
                    if(item<30):
                        outs_age_list.append(0.0)
                    elif(item<60):
                        outs_age_list.append(45.0)
                    else:
                        outs_age_list.append(100.0)
                preds_age=torch.tensor(outs_age_list).to(device)
                
                
                loss_item_age, loss_item_mask_gender = criterion_age(outs_age, age_value).item(),criterion_mask_gender(outs_mask_gender, mask_gender_label).item()
                acc=(age_value==preds_age) & (mask_gender_label==preds_mask_gender)
                acc_item = acc.sum().item()
                acc_age=(age_value==preds_age)
                acc_item_age=acc_age.sum().item()
                
                val_loss_items_age.append(loss_item_age)
                val_loss_items_mask_gender.append(loss_item_mask_gender)
                val_acc_items.append(acc_item)
                val_acc_items_age.append(acc_item_age)
                
                

                # preds=MaskBaseDataset.encode_multi_class(preds_mask,preds_gender,preds_age)

                # if figure is None:
                #     inputs_np = (
                #         torch.clone(inputs).detach().cpu().permute(0, 2, 3, 1).numpy()
                #     )
                #     inputs_np = dataset_module.denormalize_image(
                #         inputs_np, dataset.mean, dataset.std
                #     )
                #     figure = grid_image(
                #         inputs_np,
                #         labels,
                #         preds,
                #         n=16,
                #         shuffle=args.dataset != "MaskSplitByProfileDataset",
                #     )

            val_loss_age = np.sum(val_loss_items_age) / len(val_loader)
            val_loss_mask_gender = np.sum(val_loss_items_mask_gender) / len(val_loader)
            
            val_loss = val_loss_age+val_loss_mask_gender#loss_mask + loss_age + loss_gender
            
            val_acc = np.sum(val_acc_items) / len(val_set)
            val_acc_age = np.sum(val_acc_items_age) / len(val_set)
            best_val_loss = min(best_val_loss, val_loss)
            if val_acc > best_val_acc:
                best_epoch = epoch
                print(
                    f"New best model for val accuracy : {val_acc:4.2%}! saving the best model.."
                )
                torch.save(
                    {
                        'epoch': epoch,
                        'model_state_dict_mask_gender': model_mask_gender.state_dict(),
                        'optimizer_state_dict_mask_gender': optimizer_mask_gender.state_dict(),
                        'model_state_dict_age': model_age.state_dict(),
                        'optimizer_state_dict_age': optimizer_age.state_dict(),
                        'accuracy': val_acc,
                    }
                    , f"{save_dir}/best.pth")
                best_val_acc = val_acc
            if val_acc_age > best_val_age_acc:
                best_epoch_age = epoch
                torch.save(
                    {
                        'epoch': epoch,
                        'model_state_dict_mask_gender': model_mask_gender.state_dict(),
                        'optimizer_state_dict_mask_gender': optimizer_mask_gender.state_dict(),
                        'model_state_dict_age': model_age.state_dict(),
                        'optimizer_state_dict_age': optimizer_age.state_dict(),
                        'accuracy': val_acc,
                    }
                    , f"{save_dir}/best_age.pth")
                best_val_age_acc = val_acc_age
            torch.save(
                    {
                        'epoch': epoch,
                        'model_state_dict_mask_gender': model_mask_gender.state_dict(),
                        'optimizer_state_dict_mask_gender': optimizer_mask_gender.state_dict(),
                        'model_state_dict_age': model_age.state_dict(),
                        'optimizer_state_dict_age': optimizer_age.state_dict(),
                        'accuracy': val_acc,
                    }
                    , f"{save_dir}/last.pth")

            print(
                f"[Val] acc : {val_acc:4.2%}, age loss: {val_loss_age:4.2} || mask/gender loss: {val_loss_mask_gender:4.2}  "
                f"best acc : {best_val_acc:4.2%}, best loss: {best_val_loss:4.2}"
            )
            logger.add_scalar("Val/loss", val_loss, epoch)
            logger.add_scalar("Val/accuracy", val_acc, epoch)
            # logger.add_figure("results", figure, epoch)
            print()

    ################## 
    os.rename(f"{save_dir}/best.pth",f"{save_dir}/best_epoch{best_epoch:03d}.pth")
    os.rename(f"{save_dir}/last.pth",f"{save_dir}/last_epoch{args.epochs-1:03d}.pth")
    os.rename(f"{save_dir}/best_age.pth",f"{save_dir}/best_age_epoch{best_epoch_age:03d}.pth")
    ##################

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Data and model checkpoints directories
    parser.add_argument(
        "--resume_from", type=str, help="path of model to resume training"
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="random seed (default: 42)"
    )
    parser.add_argument(
        "--epochs", type=int, default=10, help="number of epochs to train (default: 10)"
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="MaskSplitByProfileDataset",
        help="dataset augmentation type (default: MaskSplitByProfileDataset)",
    )
    parser.add_argument(
        "--augmentation",
        type=str,
        default="BaseAugmentation",
        help="data augmentation type (default: BaseAugmentation)",
    )
    parser.add_argument(
        "--resize",
        nargs=2,
        type=int,
        default=[236,236],#[128, 96],
        help="resize size for image when training",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=64,
        help="input batch size for training (default: 64)",
    )
    parser.add_argument(
        "--valid_batch_size",
        type=int,
        default=1000,
        help="input batch size for validing (default: 1000)",
    )
    parser.add_argument(
        "--model", type=str, default="BaseModel", help="model type (default: BaseModel)"
    )
    parser.add_argument(
        "--optimizer", type=str, default="SGD", help="optimizer type (default: SGD)"
    )
    parser.add_argument(
        "--lr", type=float, default=1e-3, help="learning rate (default: 1e-3)"
    )
    parser.add_argument(
        "--val_ratio",
        type=float,
        default=0.2,
        help="ratio for validaton (default: 0.2)",
    )
    parser.add_argument(
        "--criterion_age",
        type=str,
        default="MSE",
        help="criterion type for age(default: f1)",
    )
    parser.add_argument(
        "--criterion_mask_gender",
        type=str,
        default="f1",
        help="criterion type for mask(default: f1)",
    )
    parser.add_argument(
        "--lr_decay_step",
        type=int,
        default=20,
        help="learning rate scheduler deacy step (default: 20)",
    )
    parser.add_argument(
        "--log_interval",
        type=int,
        default=20,
        help="how many batches to wait before logging training status",
    )
    parser.add_argument(
        "--name", default="exp", help="model save at {SM_MODEL_DIR}/{name}"
    )

    # Container environment
    parser.add_argument(
        "--data_dir",
        type=str,
        default=os.environ.get("SM_CHANNEL_TRAIN", "../../../train/images"),
    )
    parser.add_argument(
        "--model_dir", type=str, default=os.environ.get("SM_MODEL_DIR", "./model")
    )

    args = parser.parse_args()
    print(args)

    data_dir = args.data_dir
    model_dir = args.model_dir

    train(data_dir, model_dir, args)