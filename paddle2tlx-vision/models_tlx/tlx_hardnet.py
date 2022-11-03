import tensorlayerx as tlx
import collections

import paddle

from utils.download import get_weights_path_from_url
from utils.load_model import restore_model
from .ops.tlx_conv import tlx_GroupConv2d
MODEL_URLS = {
    'HarDNet39_ds':
    'https://paddle-imagenet-models-name.bj.bcebos.com/dygraph/HarDNet39_ds_pretrained.pdparams',
    'HarDNet85':
    'https://paddle-imagenet-models-name.bj.bcebos.com/dygraph/HarDNet85_pretrained.pdparams'
}

__all__ = MODEL_URLS.keys()


def ConvLayer(in_channels,
              out_channels,
              kernel_size=3,
              stride=1,
              bias_attr=False):
    layers = [('conv', tlx_GroupConv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=kernel_size // 2,
            n_group=1,
            data_format='channels_first',
            b_init=None)),   # ===============================================================
        ('norm', tlx.nn.BatchNorm2d(num_features=out_channels, data_format='channels_first')),
        ('relu', tlx.nn.ReLU6())]
    # layer = tlx.nn.Sequential(
    #     ('conv', tlx.nn.GroupConv2d(
    #         in_channels=in_channels,
    #         out_channels=out_channels,
    #         kernel_size=kernel_size,
    #         stride=stride,
    #         padding=kernel_size // 2,
    #         n_group=1,
    #         data_format='channels_first',
    #         b_init=None)),
    #     ('norm', tlx.nn.BatchNorm2d(num_features=out_channels, data_format='channels_first')),
    #     ('relu', tlx.nn.ReLU6()))
    layer = tlx.nn.Sequential(collections.OrderedDict(layers))
    return layer


def DWConvLayer(in_channels,
                out_channels,
                kernel_size=3,
                stride=1,
                bias_attr=False):
    if not bias_attr:
        bias_attr=None
    # layer = tlx.nn.Sequential( 
    #     ('dwconv', tlx.nn.GroupConv2d(
    #         in_channels=in_channels,
    #         out_channels=out_channels,
    #         kernel_size=kernel_size,
    #         stride=stride,
    #         padding=1,
    #         n_group=out_channels,
    #         data_format='channels_first',
    #         b_init=bias_attr)), 
    #     ('norm', tlx.nn.BatchNorm2d(num_features=out_channels, data_format='channels_first')))
    layers = [('dwconv', tlx_GroupConv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=1,
            n_group=out_channels,
            data_format='channels_first',
            b_init=bias_attr)),
            ('norm', tlx.nn.BatchNorm2d(num_features=out_channels, data_format='channels_first'))
    ]
    layer = tlx.nn.Sequential(collections.OrderedDict(layers))
    return layer


def CombConvLayer(in_channels, out_channels, kernel_size=1, stride=1):
    # layer = tlx.nn.Sequential(
    #     ('layer1', ConvLayer(
    #         in_channels, out_channels, kernel_size=kernel_size)),
    #     ('layer2', DWConvLayer(
    #         out_channels, out_channels, stride=stride)))
    layers = [('layer1', (ConvLayer(
            in_channels, out_channels, kernel_size=kernel_size))),
            ('layer2', (DWConvLayer(
            out_channels, out_channels, stride=stride)))]
    # layer = tlx.nn.Sequential(
    #     (ConvLayer(
    #         in_channels, out_channels, kernel_size=kernel_size)),
    #     (DWConvLayer(
    #         out_channels, out_channels, stride=stride)))
    layer = tlx.nn.Sequential(collections.OrderedDict(layers))
    return layer


class HarDBlock(tlx.nn.Module):
    def __init__(self,
                 in_channels,
                 growth_rate,
                 grmul,
                 n_layers,
                 keepBase=False,
                 residual_out=False,
                 dwconv=False):
        super().__init__()
        self.keepBase = keepBase
        self.links = []
        layers_ = []
        self.out_channels = 0  # if upsample else in_channels
        for i in range(n_layers):
            outch, inch, link = self.get_link(i + 1, in_channels, growth_rate,
                                              grmul)
            self.links.append(link)
            if dwconv:
                layers_.append(CombConvLayer(inch, outch))
            else:
                layers_.append(ConvLayer(inch, outch))

            if (i % 2 == 0) or (i == n_layers - 1):
                self.out_channels += outch
        # print("Blk out =",self.out_channels)
        self.layers = tlx.nn.ModuleList(layers_)

    def get_link(self, layer, base_ch, growth_rate, grmul):
        if layer == 0:
            return base_ch, 0, []
        out_channels = growth_rate

        link = []
        for i in range(10):
            dv = 2**i
            if layer % dv == 0:
                k = layer - dv
                link.append(k)
                if i > 0:
                    out_channels *= grmul

        out_channels = int(int(out_channels + 1) / 2) * 2
        in_channels = 0

        for i in link:
            ch, _, _ = self.get_link(i, base_ch, growth_rate, grmul)
            in_channels += ch

        return out_channels, in_channels, link

    def forward(self, x):
        layers_ = [x]

        for layer in range(len(self.layers)):
            link = self.links[layer]
            tin = []
            for i in link:
                tin.append(layers_[i])
            if len(tin) > 1:
                x = tlx.concat(tin, axis=1)
            else:
                x = tin[0]
            out = self.layers[layer](x)
            layers_.append(out)

        t = len(layers_)
        out_ = []
        for i in range(t):
            if (i == 0 and self.keepBase) or (i == t - 1) or (i % 2 == 1):
                out_.append(layers_[i])
        out = tlx.concat(out_, axis=1)

        return out


class HarDNet(tlx.nn.Module):
    def __init__(self,
                 depth_wise=False,
                 arch=85,
                 class_num=1000,
                 with_pool=True):
        super().__init__()
        first_ch = [32, 64]
        second_kernel = 3
        max_pool = True
        grmul = 1.7
        drop_rate = 0.1

        # HarDNet68
        ch_list = [128, 256, 320, 640, 1024]
        gr = [14, 16, 20, 40, 160]
        n_layers = [8, 16, 16, 16, 4]
        downSamp = [1, 0, 1, 1, 0]

        if arch == 85:
            # HarDNet85
            first_ch = [48, 96]
            ch_list = [192, 256, 320, 480, 720, 1280]
            gr = [24, 24, 28, 36, 48, 256]
            n_layers = [8, 16, 16, 16, 16, 4]
            downSamp = [1, 0, 1, 0, 1, 0]
            drop_rate = 0.2

        elif arch == 39:
            # HarDNet39
            first_ch = [24, 48]
            ch_list = [96, 320, 640, 1024]
            grmul = 1.6
            gr = [16, 20, 64, 160]
            n_layers = [4, 16, 8, 4]
            downSamp = [1, 1, 1, 0]

        if depth_wise:
            second_kernel = 1
            max_pool = False
            drop_rate = 0.05

        blks = len(n_layers)
        self.base = tlx.nn.ModuleList([])
        
        my_layers = []

        # First Layer: Standard Conv3x3, Stride=2
        self.base.append(
            ConvLayer(
                in_channels=3,
                out_channels=first_ch[0],
                kernel_size=3,
                stride=2,
                bias_attr=False))
        # my_layers.append(ConvLayer(
                # in_channels=3,
                # out_channels=first_ch[0],
                # kernel_size=3,
                # stride=2,
                # bias_attr=False))

        # Second Layer
        self.base.append(
        #     ConvLayer(
        #         first_ch[0], first_ch[1], kernel_size=second_kernel))
        # my_layers.append(
            ConvLayer(
                first_ch[0], first_ch[1], kernel_size=second_kernel))

        # Maxpooling or DWConv3x3 downsampling
        if max_pool:
            self.base.append(tlx.nn.MaxPool2d(kernel_size=3, stride=2, padding=1, data_format='channels_first'))  
            # my_layers.append(tlx.nn.MaxPool2d(kernel_size=3, stride=2, padding=1, data_format='channels_first'))  
        else:
            self.base.append(DWConvLayer(first_ch[1], first_ch[1], stride=2))
            # my_layers.append(DWConvLayer(first_ch[1], first_ch[1], stride=2))

        # Build all HarDNet blocks
        ch = first_ch[1]
        for i in range(blks):
            blk = HarDBlock(ch, gr[i], grmul, n_layers[i], dwconv=depth_wise)
            ch = blk.out_channels
            self.base.append(blk)
            # my_layers.append(blk)

            if i == blks - 1 and arch == 85:
                self.base.append(tlx.nn.Dropout(0.1))
                # my_layers.append(tlx.nn.Dropout(0.1))

            self.base.append(ConvLayer(ch, ch_list[i], kernel_size=1))
            # my_layers.append(ConvLayer(ch, ch_list[i], kernel_size=1))
            ch = ch_list[i]
            if downSamp[i] == 1:
                if max_pool:
                    self.base.append(tlx.nn.MaxPool2d(kernel_size=2, stride=2, data_format='channels_first'))
                    # my_layers.append(tlx.nn.MaxPool2d(kernel_size=2, stride=2, data_format='channels_first'))
                else:
                    self.base.append(DWConvLayer(ch, ch, stride=2))
                    # my_layers.append(DWConvLayer(ch, ch, stride=2))

        ch = ch_list[blks - 1]
        # self.base = tlx.nn.ModuleList([*my_layers])

        layers = []

        if with_pool:
            layers.append(tlx.nn.AdaptiveAvgPool2d((1, 1), data_format='channels_first'))

        if class_num > 0:
            layers.append(tlx.nn.Flatten(name='flatten'))
            layers.append(tlx.nn.Dropout(drop_rate))
            layers.append(tlx.nn.Linear(in_features=ch, out_features=class_num))

        self.base.append(tlx.nn.Sequential(*layers))

    def forward(self, x):
        for layer in self.base:
            x = layer(x)
        # print(f"hardnet.x.shape={x.shape}")
        return x



def _hardnet39_ds(arch, pretrained, **kwargs):
    model = HarDNet(arch=39, depth_wise=True, **kwargs)

    if pretrained:
        assert arch in MODEL_URLS, "{} model do not have a pretrained model now, you should set pretrained=False".format(
            arch)
        weight_path = get_weights_path_from_url(MODEL_URLS[arch])

        param = paddle.load(weight_path)
        restore_model(param, model)

    return model

def _hardnet85(arch, pretrained, **kwargs):
    model = HarDNet(arch=85, **kwargs)
    
    if pretrained:
        assert arch in MODEL_URLS, "{} model do not have a pretrained model now, you should set pretrained=False".format(
            arch)
        weight_path = get_weights_path_from_url(MODEL_URLS[arch])
        param = paddle.load(weight_path)
        restore_model(param, model)
        
    return model


def hardnet39(pretrained=False, **kwargs):
    return _hardnet39_ds('HarDNet39_ds', pretrained, **kwargs)

def hardnet85(pretrained=False, **kwargs):
    return _hardnet85('HarDNet85', pretrained, **kwargs)
