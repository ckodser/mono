# train.py
# !/usr/bin/env	python3

""" train network using pytorch

author baiyu
"""

import os
import sys
import argparse
import time
from datetime import datetime

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms

from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from conf import settings
from utils import get_network, get_training_dataloader, get_test_dataloader, WarmUpLR, \
    most_recent_folder, most_recent_weights, last_epoch, best_acc_weights, Cifar100_with_CLIP_embedding


def train(epoch):
    start = time.time()
    net.train()
    for batch_index, (images, labels) in enumerate(cifar100_training_loader):

        if args.gpu:
            labels = labels.cuda()
            images = images.cuda()

        optimizer.zero_grad()
        outputs = net(images)
        loss = loss_function(outputs, labels)
        loss.backward()
        optimizer.step()

        n_iter = (epoch - 1) * len(cifar100_training_loader) + batch_index + 1

        last_layer = list(net.children())[-1]
        # for name, para in last_layer.named_parameters():
        #     if 'weight' in name:
        #         writer.add_scalar('LastLayerGradients/grad_norm2_weights', para.grad.norm(), n_iter)
        #     if 'bias' in name:
        #         writer.add_scalar('LastLayerGradients/grad_norm2_bias', para.grad.norm(), n_iter)

        print('Training Epoch: {epoch} [{trained_samples}/{total_samples}]\tLoss: {:0.4f}\tLR: {:0.6f}'.format(
            loss.item(),
            optimizer.param_groups[0]['lr'],
            epoch=epoch,
            trained_samples=batch_index * args.b + len(images),
            total_samples=len(cifar100_training_loader.dataset)
        ))

        # update training loss for each iteration
        # writer.add_scalar('Train/loss', loss.item(), n_iter)

        if epoch <= args.warm:
            warmup_scheduler.step()

    for name, param in net.named_parameters():
        layer, attr = os.path.splitext(name)
        attr = attr[1:]
        # writer.add_histogram("{}/{}".format(layer, attr), param, epoch)

    finish = time.time()

    print('epoch {} training time consumed: {:.2f}s'.format(epoch, finish - start))


def monotrain(epoch, alpha=0.3):
    start = time.time()
    net.train()
    for batch_index, ((images, clip_embeddings), labels) in enumerate(cifar100_training_loader):
        if args.gpu:
            labels = labels.cuda()
            images = images.cuda()
            clip_embeddings = clip_embeddings.cuda()

        optimizer.zero_grad()
        outputs, l = net(images, clip_embeddings)
        classification_loss = loss_function(outputs, labels)
        loss = classification_loss + l * alpha
        loss.backward()
        optimizer.step()

        n_iter = (epoch - 1) * len(cifar100_training_loader) + batch_index + 1

        # last_layer = list(net.children())[-1]
        # for name, para in last_layer.named_parameters():
        #     if 'weight' in name:
        #         writer.add_scalar('LastLayerGradients/grad_norm2_weights', para.grad.norm(), n_iter)
        #     if 'bias' in name:
        #         writer.add_scalar('LastLayerGradients/grad_norm2_bias', para.grad.norm(), n_iter)

        print(
            'Training Epoch: {epoch} [{trained_samples}/{total_samples}]\tClassification Loss: {:0.4f}\tClip Loss: {:0.8f}\tLR: {:0.6f}'.format(
                classification_loss.item(),
                l.item(),
                optimizer.param_groups[0]['lr'],
                epoch=epoch,
                trained_samples=batch_index * args.b + len(images),
                total_samples=len(cifar100_training_loader.dataset)
            ))

        # update training loss for each iteration
        # writer.add_scalar('Train/loss', loss.item(), n_iter)

        if epoch <= args.warm:
            warmup_scheduler.step()

    for name, param in net.named_parameters():
        layer, attr = os.path.splitext(name)
        attr = attr[1:]
        # writer.add_histogram("{}/{}".format(layer, attr), param, epoch)

    finish = time.time()

    print('epoch {} training time consumed: {:.2f}s'.format(epoch, finish - start))


@torch.no_grad()
def eval_training(epoch=0, tb=True):
    start = time.time()
    net.eval()

    test_loss = 0.0  # cost function error
    correct = 0.0
    monoloss = 0.0
    activations = []
    predictions = []
    for (images, labels) in cifar100_test_loader:

        if args.gpu:
            if args.mono:
                a, b = images
                images = (a.cuda(), b.cuda())
            else:
                images = images.cuda()
            labels = labels.cuda()

        if args.mono:
            im, clipembedding = images
            outputs, ac = net(im, clipembedding, activations=True)
            for i, (activation, prediction) in enumerate(ac):
                if len(activations) <= i:
                    activations.append(activation)
                    predictions.append(prediction)
                else:
                    activations[i] = torch.cat((activations[i], activation), dim=0)
                    predictions[i] = torch.cat((predictions[i], prediction), dim=0)

        else:
            outputs = net(images)

        loss = loss_function(outputs, labels)

        test_loss += loss.item()
        _, preds = outputs.max(1)
        correct += preds.eq(labels).sum()

    if args.mono:
        def confusion_matrix(target, prediction):
            tp = torch.sum((target == 1) & (prediction == 1)).item()
            tn = torch.sum((target == 0) & (prediction == 0)).item()
            fp = torch.sum((target == 0) & (prediction == 1)).item()
            fn = torch.sum((target == 1) & (prediction == 0)).item()

            return (tn, fp), (fn, tp)

        for name, param in net.named_parameters():
            if "_part_b" in name:
                print(f"{name}| mean: {param.mean().item()}, std: {param.std().item()}")

        for i in range(len(activations)):
            print(f"Layer {i} |top5% ", end="")
            activation = activations[i]  # torch.concatinate(activations, dim=1)
            prediction = predictions[i]  # torch.concatinate(predictions, dim=1)

            num_elements = activation.size(0)
            top_k = int(0.05 * num_elements)

            _, top_k_indices = torch.topk(activation, top_k, dim=0)
            modified_activation = torch.zeros_like(activation)
            modified_activation.scatter_(0, top_k_indices, 1)

            _, top_k_indices = torch.topk(prediction, top_k, dim=0)
            modified_prediction = torch.zeros_like(prediction)
            modified_prediction.scatter_(0, top_k_indices, 1)

            (tn, fp), (fn, tp) = confusion_matrix(modified_activation.to(torch.int).flatten(),
                                                  modified_prediction.to(torch.int).flatten())
            total_samples = tn + fp + fn + tp
            print("tn:{:.4f}, fp:{:.4f}, fn:{:.4f}, tp:{:.4f} | top1% ".format(
                tn / total_samples,
                fp / total_samples,
                fn / total_samples,
                tp / total_samples
            ), end="")

            top_k = int(0.01 * num_elements)

            _, top_k_indices = torch.topk(activation, top_k, dim=0)
            modified_activation = torch.zeros_like(activation)
            modified_activation.scatter_(0, top_k_indices, 1)

            _, top_k_indices = torch.topk(prediction, top_k, dim=0)
            modified_prediction = torch.zeros_like(prediction)
            modified_prediction.scatter_(0, top_k_indices, 1)

            (tn, fp), (fn, tp) = confusion_matrix(modified_activation.to(torch.int).flatten(),
                                                  modified_prediction.to(torch.int).flatten())
            total_samples = tn + fp + fn + tp
            print("tn:{:.4f}, fp:{:.4f}, fn:{:.4f}, tp:{:.4f} | top10%".format(
                tn / total_samples,
                fp / total_samples,
                fn / total_samples,
                tp / total_samples))

            top_k = int(0.1 * num_elements)

            _, top_k_indices = torch.topk(activation, top_k, dim=0)
            modified_activation = torch.zeros_like(activation)
            modified_activation.scatter_(0, top_k_indices, 1)

            _, top_k_indices = torch.topk(prediction, top_k, dim=0)
            modified_prediction = torch.zeros_like(prediction)
            modified_prediction.scatter_(0, top_k_indices, 1)

            (tn, fp), (fn, tp) = confusion_matrix(modified_activation.to(torch.int).flatten(),
                                                  modified_prediction.to(torch.int).flatten())
            total_samples = tn + fp + fn + tp
            print("tn:{:.4f}, fp:{:.4f}, fn:{:.4f}, tp:{:.4f}".format(
                tn / total_samples,
                fp / total_samples,
                fn / total_samples,
                tp / total_samples))

    finish = time.time()
    if args.gpu:
        print('GPU INFO.....')
        print(torch.cuda.memory_summary(), end='')
    print('Evaluating Network.....')
    print(
        'Test set: Epoch: {}, Average Mono loss: {:.8f}, Average loss: {:.4f}, Accuracy: {:.4f}, Time consumed:{:.2f}s'.format(
            epoch,
            monoloss / len(cifar100_test_loader.dataset),
            test_loss / len(cifar100_test_loader.dataset),
            correct.float() / len(cifar100_test_loader.dataset),
            finish - start
        ))
    print()

    # add informations to tensorboard
    # if tb:
    #     writer.add_scalar('Test/Average loss', test_loss / len(cifar100_test_loader.dataset), epoch)
    #     writer.add_scalar('Test/Accuracy', correct.float() / len(cifar100_test_loader.dataset), epoch)

    return correct.float() / len(cifar100_test_loader.dataset)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('-net', type=str, required=True, help='net type')
    parser.add_argument('-gpu', action='store_true', default=False, help='use gpu or not')
    parser.add_argument('-b', type=int, default=128, help='batch size for dataloader')
    parser.add_argument('-warm', type=int, default=1, help='warm up training phase')
    parser.add_argument('-lr', type=float, default=0.1, help='initial learning rate')
    parser.add_argument('-resume', action='store_true', default=False, help='resume training')
    parser.add_argument('-mono', action='store_true', default=False, help='monosematicisty')
    parser.add_argument('-alpha', type=float, default=0.3, help='alpha for monosematicisty')
    args = parser.parse_args()

    net = get_network(args)

    # dataset
    if args.mono:
        dataset = Cifar100_with_CLIP_embedding
    else:
        dataset = torchvision.datasets.CIFAR100

    # data preprocessing:
    cifar100_training_loader = get_training_dataloader(
        settings.CIFAR100_TRAIN_MEAN,
        settings.CIFAR100_TRAIN_STD,
        num_workers=4,
        batch_size=args.b,
        shuffle=True,
        dataset_class=dataset
    )

    cifar100_test_loader = get_test_dataloader(
        settings.CIFAR100_TRAIN_MEAN,
        settings.CIFAR100_TRAIN_STD,
        num_workers=4,
        batch_size=args.b,
        shuffle=True,
        dataset_class=dataset
    )

    loss_function = nn.CrossEntropyLoss()
    optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=0.9, weight_decay=5e-4)
    train_scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=settings.MILESTONES,
                                                     gamma=0.2)  # learning rate decay
    iter_per_epoch = len(cifar100_training_loader)
    warmup_scheduler = WarmUpLR(optimizer, iter_per_epoch * args.warm)

    if args.resume:
        recent_folder = most_recent_folder(os.path.join(settings.CHECKPOINT_PATH, args.net), fmt=settings.DATE_FORMAT)
        if not recent_folder:
            raise Exception('no recent folder were found')

        checkpoint_path = os.path.join(settings.CHECKPOINT_PATH, args.net, recent_folder)

    else:
        checkpoint_path = os.path.join(settings.CHECKPOINT_PATH, args.net, settings.TIME_NOW)

    # use tensorboard
    if not os.path.exists(settings.LOG_DIR):
        os.mkdir(settings.LOG_DIR)

    # since tensorboard can't overwrite old values
    # so the only way is to create a new tensorboard log
    # writer = SummaryWriter(log_dir=os.path.join(
    #     settings.LOG_DIR, args.net, settings.TIME_NOW))
    # if args.mono:
    #     if not args.gpu:
    #         input_tensor = (torch.Tensor(1, 3, 32, 32), torch.Tensor(1, 728))
    #     if args.gpu:
    #         input_tensor = (torch.Tensor(1, 3, 32, 32).cuda(), torch.Tensor(1, 728).cuda())
    # else:
    #     input_tensor = torch.Tensor(1, 3, 32, 32)
    #     if args.gpu:
    #         input_tensor = input_tensor.cuda()
    # writer.add_graph(net, input_tensor)

    # create checkpoint folder to save model
    if not os.path.exists(checkpoint_path):
        os.makedirs(checkpoint_path)
    checkpoint_path = os.path.join(checkpoint_path, '{net}-{epoch}-{type}.pth')

    best_acc = 0.0
    if args.resume:
        best_weights = best_acc_weights(os.path.join(settings.CHECKPOINT_PATH, args.net, recent_folder))
        if best_weights:
            weights_path = os.path.join(settings.CHECKPOINT_PATH, args.net, recent_folder, best_weights)
            print('found best acc weights file:{}'.format(weights_path))
            print('load best training file to test acc...')
            net.load_state_dict(torch.load(weights_path))
            best_acc = eval_training(tb=False)
            print('best acc is {:0.2f}'.format(best_acc))

        recent_weights_file = most_recent_weights(os.path.join(settings.CHECKPOINT_PATH, args.net, recent_folder))
        if not recent_weights_file:
            raise Exception('no recent weights file were found')
        weights_path = os.path.join(settings.CHECKPOINT_PATH, args.net, recent_folder, recent_weights_file)
        print('loading weights file {} to resume training.....'.format(weights_path))
        net.load_state_dict(torch.load(weights_path))

        resume_epoch = last_epoch(os.path.join(settings.CHECKPOINT_PATH, args.net, recent_folder))

    for epoch in range(1, settings.EPOCH + 1):
        if epoch > args.warm:
            train_scheduler.step(epoch)

        if args.resume:
            if epoch <= resume_epoch:
                continue
        if args.mono:
            monotrain(epoch, alpha=args.alpha)
        else:
            train(epoch)
        acc = eval_training(epoch)

        # start to save best performance model after learning rate decay to 0.01
        if epoch > settings.MILESTONES[1] and best_acc < acc:
            weights_path = checkpoint_path.format(net=args.net, epoch=epoch, type='best')
            print('saving weights file to {}'.format(weights_path))
            torch.save(net.state_dict(), weights_path)
            best_acc = acc
            continue

        if not epoch % settings.SAVE_EPOCH:
            weights_path = checkpoint_path.format(net=args.net, epoch=epoch, type='regular')
            print('saving weights file to {}'.format(weights_path))
            torch.save(net.state_dict(), weights_path)

    # writer.close()
