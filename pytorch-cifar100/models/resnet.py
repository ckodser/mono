"""resnet in pytorch



[1] Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun.

    Deep Residual Learning for Image Recognition
    https://arxiv.org/abs/1512.03385v1
"""

import torch
import torch.nn as nn


class BasicBlock(nn.Module):
    """Basic Block for resnet 18 and resnet 34

    """

    # BasicBlock and BottleNeck block
    # have different output size
    # we use class attribute expansion
    # to distinct
    expansion = 1

    def __init__(self, in_channels, out_channels, stride=1):
        super().__init__()

        # residual function
        self.residual_function = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=stride, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels * BasicBlock.expansion, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels * BasicBlock.expansion)
        )

        # shortcut
        self.shortcut = nn.Sequential()

        # the shortcut output dimension is not the same with residual function
        # use 1*1 convolution to match the dimension
        if stride != 1 or in_channels != BasicBlock.expansion * out_channels:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_channels, out_channels * BasicBlock.expansion, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(out_channels * BasicBlock.expansion)
            )

    def forward(self, x):
        return nn.ReLU(inplace=True)(self.residual_function(x) + self.shortcut(x))


class top_k_percent_one_side(nn.Module):
    def __init__(self, k):
        super().__init__()
        self.k = k

    def forward(self, output, target):
        with torch.no_grad():
            num_elements = target.size(0)
            top_k = int(self.k * num_elements)
            _, top_k_indices = torch.topk(target, top_k, dim=0)
            modified_target = torch.zeros_like(target)
            modified_target.scatter_(0, top_k_indices, 1)

        loss_fn = nn.BCEWithLogitsLoss(weight=(modified_target.flatten() * (1 / self.k - 2) + 1) / 100)
        loss = loss_fn(output.flatten(), modified_target.flatten())

        return loss


class kllogit(nn.Module):
    def __init__(self):
        super().__init__()
        self.logsigmoid = nn.LogSigmoid()
        self.loss = torch.nn.KLDivLoss(log_target=True)

    def forward(self, activation, prediction):
        activationlogsig = self.logsigmoid(activation)
        predictionlogsig = self.logsigmoid(prediction)
        return self.loss(predictionlogsig, activationlogsig)


class top_k_percent_two_side(nn.Module):
    def __init__(self, k):
        super().__init__()
        self.topk = top_k_percent_one_side(k)
        # self.loss = nn.MSELoss()
        self.loss = kllogit()

    def forward(self, activation, prediction):
        return self.topk(activation, prediction) + self.topk(prediction, activation)



class MonoBasicBlock(nn.Module):
    """Basic Block for resnet 18 and resnet 34

    """

    # BasicBlock and BottleNeck block
    # have different output size
    # we use class attribute expansion
    # to distinct
    expansion = 1

    def __init__(self, in_channels, out_channels, stride=1, clipd=768):
        super().__init__()

        # residual function
        self.residual_function_first_part = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=stride, padding=1, bias=False))
        self.residual_function_first_part_b = nn.Parameter(torch.zeros(out_channels))

        self.residual_feature_first_part = nn.Sequential(
            nn.Linear(clipd, out_channels),
        )
        self.residual_function_second_part = nn.Sequential(
            nn.BatchNorm2d(out_channels),
            nn.ReLU(),
            nn.Conv2d(out_channels, out_channels * BasicBlock.expansion, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels * BasicBlock.expansion)
        )
        self.residual_function_second_part_b = nn.Parameter(torch.zeros(out_channels * BasicBlock.expansion))
        self.residual_feature_whole_part = nn.Sequential(
            nn.Linear(clipd, out_channels * BasicBlock.expansion),
        )
        # shortcut
        self.shortcut = nn.Sequential()

        # the shortcut output dimension is not the same with residual function
        # use 1*1 convolution to match the dimension
        if stride != 1 or in_channels != BasicBlock.expansion * out_channels:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_channels, out_channels * BasicBlock.expansion, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(out_channels * BasicBlock.expansion)
            )

        self.loss = top_k_percent_two_side(0.05)

    def forward(self, x, clip_embeddings, activations=False):
        step1 = self.residual_function_first_part(x)
        x = self.residual_function_second_part(step1) + self.shortcut(x)
        fx = nn.ReLU()(x)
        # print("step1.shape:", step1.flatten(start_dim=2).mean(dim=2).shape, "pred.shape:", self.residual_feature_first_part(clip_embeddings).shape)
        # print("step2.shape:", x.flatten(start_dim=2).mean(dim=2).shape, "pred.shape:", self.residual_feature_whole_part(clip_embeddings).shape)
        # print()
        if activations:
            return (fx,
                    [[step1.flatten(start_dim=2).mean(dim=2) + self.residual_function_first_part_b,
                      self.residual_feature_first_part(clip_embeddings)],
                     [x.flatten(start_dim=2).mean(dim=2) + self.residual_function_second_part_b,
                      self.residual_feature_whole_part(clip_embeddings)]]
                    )
        else:
            return (fx,
                    self.loss(step1.flatten(start_dim=2).mean(dim=2) + self.residual_function_first_part_b,
                              self.residual_feature_first_part(clip_embeddings)) + \
                    self.loss(x.flatten(start_dim=2).mean(dim=2) + self.residual_function_second_part_b,
                              self.residual_feature_whole_part(clip_embeddings))
                    )


class MonoSequential(nn.Module):
    def __init__(self, layers):
        super().__init__()
        self.layers = nn.ModuleList(layers)

    def forward(self, x, clip_embeddings, activations=False):
        if activations:
            l = []
        else:
            l = 0.0
        for layer in self.layers:
            x, ln = layer(x, clip_embeddings, activations=activations)
            l += ln
        return x, l


class BottleNeck(nn.Module):
    """Residual block for resnet over 50 layers

    """
    expansion = 4

    def __init__(self, in_channels, out_channels, stride=1):
        super().__init__()
        self.residual_function = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, stride=stride, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels * BottleNeck.expansion, kernel_size=1, bias=False),
            nn.BatchNorm2d(out_channels * BottleNeck.expansion),
        )

        self.shortcut = nn.Sequential()

        if stride != 1 or in_channels != out_channels * BottleNeck.expansion:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_channels, out_channels * BottleNeck.expansion, stride=stride, kernel_size=1, bias=False),
                nn.BatchNorm2d(out_channels * BottleNeck.expansion)
            )

    def forward(self, x):
        return nn.ReLU(inplace=True)(self.residual_function(x) + self.shortcut(x))


class ResNet(nn.Module):

    def __init__(self, block, num_block, num_classes=100):
        super().__init__()

        self.in_channels = 64

        self.conv1 = nn.Sequential(
            nn.Conv2d(3, 64, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True))
        # we use a different inputsize than the original paper
        # so conv2_x's stride is 1
        self.conv2_x = self._make_layer(block, 64, num_block[0], 1)
        self.conv3_x = self._make_layer(block, 128, num_block[1], 2)
        self.conv4_x = self._make_layer(block, 256, num_block[2], 2)
        self.conv5_x = self._make_layer(block, 512, num_block[3], 2)
        self.avg_pool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512 * block.expansion, num_classes)

    def _make_layer(self, block, out_channels, num_blocks, stride):
        """make resnet layers(by layer i didnt mean this 'layer' was the
        same as a neuron netowork layer, ex. conv layer), one layer may
        contain more than one residual block

        Args:
            block: block type, basic block or bottle neck block
            out_channels: output depth channel number of this layer
            num_blocks: how many blocks per layer
            stride: the stride of the first block of this layer

        Return:
            return a resnet layer
        """

        # we have num_block blocks per layer, the first block
        # could be 1 or 2, other blocks would always be 1
        strides = [stride] + [1] * (num_blocks - 1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_channels, out_channels, stride))
            self.in_channels = out_channels * block.expansion

        return MonoSequential(layers)

    def forward(self, x):
        output = self.conv1(x)
        output = self.conv2_x(output)
        output = self.conv3_x(output)
        output = self.conv4_x(output)
        output = self.conv5_x(output)
        output = self.avg_pool(output)
        output = output.view(output.size(0), -1)
        output = self.fc(output)

        return output


class MonoResNet(ResNet):
    def forward(self, x, clip_embedding, activations=False):
        output = self.conv1(x)
        output, l = self.conv2_x(output, clip_embedding, activations=activations)
        output, ln = self.conv3_x(output, clip_embedding, activations=activations)
        l += ln
        output, ln = self.conv4_x(output, clip_embedding, activations=activations)
        l += ln
        output, ln = self.conv5_x(output, clip_embedding, activations=activations)
        l += ln
        output = self.avg_pool(output)
        output = output.view(output.size(0), -1)
        output = self.fc(output)

        return output, l


def monoresnet18():
    """ return a ResNet 18 object
    """
    return MonoResNet(MonoBasicBlock, [2, 2, 2, 2])


def monoresnet34():
    """ return a ResNet 34 object
    """
    return MonoResNet(MonoBasicBlock, [3, 4, 6, 3])


def resnet18():
    """ return a ResNet 18 object
    """
    return ResNet(BasicBlock, [2, 2, 2, 2])


def resnet34():
    """ return a ResNet 34 object
    """
    return ResNet(BasicBlock, [3, 4, 6, 3])


def resnet50():
    """ return a ResNet 50 object
    """
    return ResNet(BottleNeck, [3, 4, 6, 3])


def resnet101():
    """ return a ResNet 101 object
    """
    return ResNet(BottleNeck, [3, 4, 23, 3])


def resnet152():
    """ return a ResNet 152 object
    """
    return ResNet(BottleNeck, [3, 8, 36, 3])
