import functools
import os
from collections import OrderedDict

import torch
import torch.nn.functional as F
import torchvision
from torch import nn

from models.arch_util import ConvBnLelu, ConvGnSilu, ExpansionBlock, ExpansionBlock2, MultiConvBlock
from models.switched_conv.switched_conv import BareConvSwitch, compute_attention_specificity, AttentionNorm
from models.switched_conv.switched_conv_util import save_attention_to_image_rgb
from trainer.networks import register_model
from utils.util import checkpoint


# VGG-style layer with Conv(stride2)->BN->Activation->Conv->BN->Activation
# Doubles the input filter count.
class HalvingProcessingBlock(nn.Module):
    def __init__(self, filters):
        super(HalvingProcessingBlock, self).__init__()
        self.bnconv1 = ConvGnSilu(filters, filters * 2, stride=2, norm=False, bias=False)
        self.bnconv2 = ConvGnSilu(filters * 2, filters * 2, norm=True, bias=False)

    def forward(self, x):
        x = self.bnconv1(x)
        return self.bnconv2(x)


# This is a classic u-net architecture with the goal of assigning each individual pixel an individual transform
# switching set.
class ConvBasisMultiplexer(nn.Module):
    def __init__(self, input_channels, base_filters, reductions, processing_depth, multiplexer_channels, use_gn=True, use_exp2=False):
        super(ConvBasisMultiplexer, self).__init__()
        self.filter_conv = ConvGnSilu(input_channels, base_filters, bias=True)
        self.reduction_blocks = nn.ModuleList([HalvingProcessingBlock(base_filters * 2 ** i) for i in range(reductions)])
        reduction_filters = base_filters * 2 ** reductions
        self.processing_blocks = nn.Sequential(OrderedDict([('block%i' % (i,), ConvGnSilu(reduction_filters, reduction_filters, bias=False)) for i in range(processing_depth)]))
        if use_exp2:
            self.expansion_blocks = nn.ModuleList([ExpansionBlock2(reduction_filters // (2 ** i)) for i in range(reductions)])
        else:
            self.expansion_blocks = nn.ModuleList([ExpansionBlock(reduction_filters // (2 ** i)) for i in range(reductions)])

        gap = base_filters - multiplexer_channels
        cbl1_out = ((base_filters - (gap // 2)) // 4) * 4   # Must be multiples of 4 to use with group norm.
        self.cbl1 = ConvGnSilu(base_filters, cbl1_out, norm=use_gn, bias=False, num_groups=4)
        cbl2_out = ((base_filters - (3 * gap // 4)) // 4) * 4
        self.cbl2 = ConvGnSilu(cbl1_out, cbl2_out, norm=use_gn, bias=False, num_groups=4)
        self.cbl3 = ConvGnSilu(cbl2_out, multiplexer_channels, bias=True, norm=False)

    def forward(self, x):
        x = self.filter_conv(x)
        reduction_identities = []
        for b in self.reduction_blocks:
            reduction_identities.append(x)
            x = b(x)
        x = self.processing_blocks(x)
        for i, b in enumerate(self.expansion_blocks):
            x = b(x, reduction_identities[-i - 1])

        x = self.cbl1(x)
        x = self.cbl2(x)
        x = self.cbl3(x)
        return x


# torch.gather() which operates across 2d images.
def gather_2d(input, index):
    b, c, h, w = input.shape
    nodim = input.view(b, c, h * w)
    ind_nd = index[:, 0]*w + index[:, 1]
    ind_nd = ind_nd.unsqueeze(1)
    ind_nd = ind_nd.repeat((1, c))
    ind_nd = ind_nd.unsqueeze(2)
    result = torch.gather(nodim, dim=2, index=ind_nd)
    result = result.squeeze()
    if b == 1:
        result = result.unsqueeze(0)
    return result


class ConfigurableSwitchComputer(nn.Module):
    def __init__(self, base_filters, multiplexer_net, pre_transform_block, transform_block, transform_count, attention_norm,
                 post_transform_block=None,
                 init_temp=20, add_scalable_noise_to_transforms=False, feed_transforms_into_multiplexer=False, post_switch_conv=True,
                 anorm_multiplier=16):
        super(ConfigurableSwitchComputer, self).__init__()

        tc = transform_count
        self.multiplexer = multiplexer_net(tc)

        if pre_transform_block:
            self.pre_transform = pre_transform_block()
        else:
            self.pre_transform = None
        self.transforms = nn.ModuleList([transform_block() for _ in range(transform_count)])
        self.add_noise = add_scalable_noise_to_transforms
        self.feed_transforms_into_multiplexer = feed_transforms_into_multiplexer
        self.noise_scale = nn.Parameter(torch.full((1,), float(1e-3)))

        # And the switch itself, including learned scalars
        self.switch = BareConvSwitch(initial_temperature=init_temp, attention_norm=AttentionNorm(transform_count, accumulator_size=anorm_multiplier * transform_count) if attention_norm else None)
        self.switch_scale = nn.Parameter(torch.full((1,), float(1)))
        self.post_transform_block = post_transform_block
        if post_switch_conv:
            self.post_switch_conv = ConvBnLelu(base_filters, base_filters, norm=False, bias=True)
            # The post_switch_conv gets a low scale initially. The network can decide to magnify it (or not)
            # depending on its needs.
            self.psc_scale = nn.Parameter(torch.full((1,), float(.1)))
        else:
            self.post_switch_conv = None
        self.update_norm = True

    def set_update_attention_norm(self, set_val):
        self.update_norm = set_val

    # Regarding inputs: it is acceptable to pass in a tuple/list as an input for (x), but the first element
    # *must* be the actual parameter that gets fed through the network - it is assumed to be the identity.
    def forward(self, x, att_in=None, identity=None, output_attention_weights=True, fixed_scale=1, do_checkpointing=False,
                output_att_logits=False):
        if isinstance(x, tuple):
            x1 = x[0]
        else:
            x1 = x

        if att_in is None:
            att_in = x

        if identity is None:
            identity = x1

        if self.add_noise:
            rand_feature = torch.randn_like(x1) * self.noise_scale
            if isinstance(x, tuple):
                x = (x1 + rand_feature,) + x[1:]
            else:
                x = x1 + rand_feature

        if not isinstance(x, tuple):
            x = (x,)
        if self.pre_transform:
            x = self.pre_transform(*x)
        if not isinstance(x, tuple):
            x = (x,)
        if do_checkpointing:
            xformed = [checkpoint(t, *x) for t in self.transforms]
        else:
            xformed = [t(*x) for t in self.transforms]

        if not isinstance(att_in, tuple):
            att_in = (att_in,)
        if self.feed_transforms_into_multiplexer:
            att_in = att_in + (torch.stack(xformed, dim=1),)
        if do_checkpointing:
            m = checkpoint(self.multiplexer, *att_in)
        else:
            m = self.multiplexer(*att_in)

        # It is assumed that [xformed] and [m] are collapsed into tensors at this point.
        outputs, attention, att_logits = self.switch(xformed, m, True, self.update_norm, output_attention_logits=True)
        if self.post_transform_block is not None:
            outputs = self.post_transform_block(outputs)

        outputs = identity + outputs * self.switch_scale * fixed_scale
        if self.post_switch_conv is not None:
            outputs = outputs + self.post_switch_conv(outputs) * self.psc_scale * fixed_scale
        if output_attention_weights:
            if output_att_logits:
                return outputs, attention, att_logits
            else:
                return outputs, attention
        else:
            return outputs

    def set_temperature(self, temp):
        self.switch.set_attention_temperature(temp)


class ConfigurableSwitchedResidualGenerator2(nn.Module):
    def __init__(self, switch_depth, switch_filters, switch_reductions, switch_processing_layers, trans_counts, trans_kernel_sizes,
                 trans_layers, transformation_filters, attention_norm, initial_temp=20, final_temperature_step=50000, heightened_temp_min=1,
                 heightened_final_step=50000, upsample_factor=1,
                 add_scalable_noise_to_transforms=False):
        super(ConfigurableSwitchedResidualGenerator2, self).__init__()
        switches = []
        self.initial_conv = ConvBnLelu(3, transformation_filters, norm=False, activation=False, bias=True)
        self.upconv1 = ConvBnLelu(transformation_filters, transformation_filters, norm=False, bias=True)
        self.upconv2 = ConvBnLelu(transformation_filters, transformation_filters, norm=False, bias=True)
        self.hr_conv = ConvBnLelu(transformation_filters, transformation_filters, norm=False, bias=True)
        self.final_conv = ConvBnLelu(transformation_filters, 3, norm=False, activation=False, bias=True)
        for _ in range(switch_depth):
            multiplx_fn = functools.partial(ConvBasisMultiplexer, transformation_filters, switch_filters, switch_reductions, switch_processing_layers, trans_counts)
            pretransform_fn = functools.partial(ConvBnLelu, transformation_filters, transformation_filters, norm=False, bias=False, weight_init_factor=.1)
            transform_fn = functools.partial(MultiConvBlock, transformation_filters, int(transformation_filters * 1.5), transformation_filters, kernel_size=trans_kernel_sizes, depth=trans_layers, weight_init_factor=.1)
            switches.append(ConfigurableSwitchComputer(transformation_filters, multiplx_fn,
                                                       pre_transform_block=pretransform_fn, transform_block=transform_fn,
                                                       attention_norm=attention_norm,
                                                       transform_count=trans_counts, init_temp=initial_temp,
                                                       add_scalable_noise_to_transforms=add_scalable_noise_to_transforms))

        self.switches = nn.ModuleList(switches)
        self.transformation_counts = trans_counts
        self.init_temperature = initial_temp
        self.final_temperature_step = final_temperature_step
        self.heightened_temp_min = heightened_temp_min
        self.heightened_final_step = heightened_final_step
        self.attentions = None
        self.upsample_factor = upsample_factor
        assert self.upsample_factor == 2 or self.upsample_factor == 4

    def forward(self, x):
        # This is a common bug when evaluating SRG2 generators. It needs to be configured properly in eval mode. Just fail.
        if not self.train:
            assert self.switches[0].switch.temperature == 1

        x = self.initial_conv(x)

        self.attentions = []
        for i, sw in enumerate(self.switches):
            x, att = sw.forward(x, True)
            self.attentions.append(att)

        x = self.upconv1(F.interpolate(x, scale_factor=2, mode="nearest"))
        if self.upsample_factor > 2:
            x = F.interpolate(x, scale_factor=2, mode="nearest")
        x = self.upconv2(x)
        x = self.final_conv(self.hr_conv(x))
        return x, x

    def set_temperature(self, temp):
        [sw.set_temperature(temp) for sw in self.switches]

    def update_for_step(self, step, experiments_path='.'):
        if self.attentions:
            temp = max(1,
                1 + self.init_temperature * (self.final_temperature_step - step) / self.final_temperature_step)
            if temp == 1 and self.heightened_final_step and step > self.final_temperature_step and \
                    self.heightened_final_step != 1:
                # Once the temperature passes (1) it enters an inverted curve to match the linear curve from above.
                # without this, the attention specificity "spikes" incredibly fast in the last few iterations.
                h_steps_total = self.heightened_final_step - self.final_temperature_step
                h_steps_current = min(step - self.final_temperature_step, h_steps_total)
                # The "gap" will represent the steps that need to be traveled as a linear function.
                h_gap = 1 / self.heightened_temp_min
                temp = h_gap * h_steps_current / h_steps_total
                # Invert temperature to represent reality on this side of the curve
                temp = 1 / temp
            self.set_temperature(temp)
            if step % 50 == 0:
                output_path = os.path.join(experiments_path, "attention_maps", "a%i")
                prefix = "attention_map_%i_%%i.png" % (step,)
                [save_attention_to_image_rgb(output_path % (i,), self.attentions[i], self.transformation_counts, prefix, step) for i in range(len(self.attentions))]

    def get_debug_values(self, step):
        temp = self.switches[0].switch.temperature
        mean_hists = [compute_attention_specificity(att, 2) for att in self.attentions]
        means = [i[0] for i in mean_hists]
        hists = [i[1].clone().detach().cpu().flatten() for i in mean_hists]
        val = {"switch_temperature": temp}
        for i in range(len(means)):
            val["switch_%i_specificity" % (i,)] = means[i]
            val["switch_%i_histogram" % (i,)] = hists[i]
        return val


# Computes a linear latent by performing processing on the reference image and returning the filters of a single point,
# which should be centered on the image patch being processed.
#
# Output is base_filters * 8.
class ReferenceImageBranch(nn.Module):
    def __init__(self, base_filters=64):
        super(ReferenceImageBranch, self).__init__()
        self.features = nn.Sequential(ConvGnSilu(4, base_filters, kernel_size=7, bias=True),
                                      HalvingProcessingBlock(base_filters),
                                      ConvGnSilu(base_filters*2, base_filters*2, activation=True, norm=True, bias=False),
                                      HalvingProcessingBlock(base_filters*2),
                                      ConvGnSilu(base_filters*4, base_filters*4, activation=True, norm=True, bias=False),
                                      HalvingProcessingBlock(base_filters*4),
                                      ConvGnSilu(base_filters*8, base_filters*8, activation=True, norm=True, bias=False),
                                      ConvGnSilu(base_filters*8, base_filters*8, activation=True, norm=True, bias=False))

    # center_point is a [b,2] long tensor describing the center point of where the patch was taken from the reference
    # image.
    def forward(self, x, center_point):
        x = self.features(x)
        return gather_2d(x, center_point // 8)  # Divide by 8 to scale the center_point down.


# Mutiplexer that combines a structured embedding with a contextual switch input to guide alterations to that input.
#
# Implemented as basically a u-net which reduces the input into the same structural space as the embedding, combines the
# two, then expands back into the original feature space.
class EmbeddingMultiplexer(nn.Module):
    # Note: reductions=2 if the encoder is using interpolated input, otherwise reductions=3.
    def __init__(self, nf, multiplexer_channels, reductions=2):
        super(EmbeddingMultiplexer, self).__init__()
        self.embedding_process = MultiConvBlock(256, 256, 256, kernel_size=3, depth=3, norm=True)

        self.filter_conv = ConvGnSilu(nf, nf, activation=True, norm=False, bias=True)
        self.reduction_blocks = nn.ModuleList([HalvingProcessingBlock(nf * 2 ** i) for i in range(reductions)])
        reduction_filters = nf * 2 ** reductions
        self.processing_blocks = nn.Sequential(
            ConvGnSilu(reduction_filters + 256, reduction_filters + 256, kernel_size=1, activation=True, norm=False, bias=True),
            ConvGnSilu(reduction_filters + 256, reduction_filters + 128, kernel_size=1, activation=True, norm=True, bias=False),
            ConvGnSilu(reduction_filters + 128, reduction_filters, kernel_size=3, activation=True, norm=True, bias=False),
            ConvGnSilu(reduction_filters, reduction_filters, kernel_size=3, activation=True, norm=True, bias=False))
        self.expansion_blocks = nn.ModuleList([ExpansionBlock2(reduction_filters // (2 ** i)) for i in range(reductions)])

        gap = nf - multiplexer_channels
        cbl1_out = ((nf - (gap // 2)) // 4) * 4   # Must be multiples of 4 to use with group norm.
        self.cbl1 = ConvGnSilu(nf, cbl1_out, norm=True, bias=False, num_groups=4)
        cbl2_out = ((nf - (3 * gap // 4)) // 4) * 4
        self.cbl2 = ConvGnSilu(cbl1_out, cbl2_out, norm=True, bias=False, num_groups=4)
        self.cbl3 = ConvGnSilu(cbl2_out, multiplexer_channels, bias=True, norm=False)

    def forward(self, x, embedding):
        x = self.filter_conv(x)
        embedding = self.embedding_process(embedding)

        reduction_identities = []
        for b in self.reduction_blocks:
            reduction_identities.append(x)
            x = b(x)
        x = self.processing_blocks(torch.cat([x, embedding], dim=1))
        for i, b in enumerate(self.expansion_blocks):
            x = b(x, reduction_identities[-i - 1])

        x = self.cbl1(x)
        x = self.cbl2(x)
        x = self.cbl3(x)
        return x


class QueryKeyMultiplexer(nn.Module):
    def __init__(self, nf, multiplexer_channels, embedding_channels=256, reductions=2):
        super(QueryKeyMultiplexer, self).__init__()

        # Blocks used to create the query
        self.input_process = ConvGnSilu(nf, nf, activation=True, norm=False, bias=True)
        self.embedding_process = ConvGnSilu(embedding_channels, 256, activation=True, norm=False, bias=True)
        self.reduction_blocks = nn.ModuleList([HalvingProcessingBlock(nf * 2 ** i) for i in range(reductions)])
        reduction_filters = nf * 2 ** reductions
        self.processing_blocks = nn.Sequential(
            ConvGnSilu(reduction_filters + 256, reduction_filters + 256, kernel_size=1, activation=True, norm=False, bias=True),
            ConvGnSilu(reduction_filters + 256, reduction_filters + 128, kernel_size=1, activation=True, norm=True, bias=False),
            ConvGnSilu(reduction_filters + 128, reduction_filters, kernel_size=3, activation=True, norm=True, bias=False),
            ConvGnSilu(reduction_filters, reduction_filters, kernel_size=3, activation=True, norm=True, bias=False))
        self.expansion_blocks = nn.ModuleList([ExpansionBlock2(reduction_filters // (2 ** i)) for i in range(reductions)])

        # Blocks used to create the key
        self.key_process = ConvGnSilu(nf, nf, kernel_size=1, activation=True, norm=False, bias=True)

        # Postprocessing blocks.
        self.query_key_combine = ConvGnSilu(nf*2, nf, kernel_size=1, activation=True, norm=False, bias=False)
        self.cbl1 = ConvGnSilu(nf, nf // 2, kernel_size=1, norm=True, bias=False, num_groups=4)
        self.cbl2 = ConvGnSilu(nf // 2, 1, kernel_size=1, norm=False, bias=False)

    def forward(self, x, embedding, transformations):
        q = self.input_process(x)
        embedding = self.embedding_process(embedding)
        reduction_identities = []
        for b in self.reduction_blocks:
            reduction_identities.append(q)
            q = b(q)
        q = self.processing_blocks(torch.cat([q, embedding], dim=1))
        for i, b in enumerate(self.expansion_blocks):
            q = b(q, reduction_identities[-i - 1])

        b, t, f, h, w = transformations.shape
        k = transformations.view(b * t, f, h, w)
        k = self.key_process(k)

        q = q.view(b, 1, f, h, w).repeat(1, t, 1, 1, 1).view(b * t, f, h, w)
        v = self.query_key_combine(torch.cat([q, k], dim=1))

        v = self.cbl1(v)
        v = self.cbl2(v)

        return v.view(b, t, h, w)


class QueryKeyPyramidMultiplexer(nn.Module):
    def __init__(self, nf, multiplexer_channels, reductions=3):
        super(QueryKeyPyramidMultiplexer, self).__init__()

        # Blocks used to create the query
        self.input_process = ConvGnSilu(nf, nf, activation=True, norm=False, bias=True)
        self.reduction_blocks = nn.ModuleList([HalvingProcessingBlock(nf * 2 ** i) for i in range(reductions)])
        reduction_filters = nf * 2 ** reductions
        self.processing_blocks = nn.Sequential(OrderedDict([('block%i' % (i,), ConvGnSilu(reduction_filters, reduction_filters, kernel_size=1, norm=True, bias=False)) for i in range(3)]))
        self.expansion_blocks = nn.ModuleList([ExpansionBlock2(reduction_filters // (2 ** i)) for i in range(reductions)])

        # Blocks used to create the key
        self.key_process = ConvGnSilu(nf, nf, kernel_size=1, activation=True, norm=False, bias=True)

        # Postprocessing blocks.
        self.query_key_combine = ConvGnSilu(nf*2, nf, kernel_size=3, activation=True, norm=False, bias=False)
        self.cbl0 = ConvGnSilu(nf, nf, kernel_size=3, activation=True, norm=True, bias=False)
        self.cbl1 = ConvGnSilu(nf, nf // 2, kernel_size=1, norm=True, bias=False, num_groups=4)
        self.cbl2 = ConvGnSilu(nf // 2, 1, kernel_size=1, norm=False, bias=False)

    def forward(self, x, transformations):
        q = self.input_process(x)
        reduction_identities = []
        for b in self.reduction_blocks:
            reduction_identities.append(q)
            q = b(q)
        q = self.processing_blocks(q)
        for i, b in enumerate(self.expansion_blocks):
            q = b(q, reduction_identities[-i - 1])

        b, t, f, h, w = transformations.shape
        k = transformations.view(b * t, f, h, w)
        k = self.key_process(k)

        q = q.view(b, 1, f, h, w).repeat(1, t, 1, 1, 1).view(b * t, f, h, w)
        v = self.query_key_combine(torch.cat([q, k], dim=1))
        v = self.cbl0(v)
        v = self.cbl1(v)
        v = self.cbl2(v)

        return v.view(b, t, h, w)


# Base class for models that utilize ConfigurableSwitchComputer. Provides basis functionality like logging
# switch temperature, distribution and images, as well as managing attention norms.
class SwitchModelBase(nn.Module):
    def __init__(self, init_temperature=10, final_temperature_step=10000):
        super(SwitchModelBase, self).__init__()
        self.switches = []  # The implementing class is expected to set this to a list of all ConfigurableSwitchComputers.
        self.attentions = []  # The implementing class is expected to set this in forward() to the output of the attention blocks.
        self.lr = None  # The implementing class is expected to set this to the input image fed into the generator. If not
                        # set, the attention logger will not output an image reference.
        self.init_temperature = init_temperature
        self.final_temperature_step = final_temperature_step

    def set_temperature(self, temp):
        [sw.set_temperature(temp) for sw in self.switches]

    def update_for_step(self, step, experiments_path='.'):
        # All-reduce the attention norm.
        for sw in self.switches:
            sw.switch.reduce_norm_params()

        temp = max(1, 1 + self.init_temperature *
                   (self.final_temperature_step - step) / self.final_temperature_step)
        self.set_temperature(temp)
        if step % 100 == 0:
            output_path = os.path.join(experiments_path, "attention_maps")
            prefix = "amap_%i_a%i_%%i.png"
            [save_attention_to_image_rgb(output_path, self.attentions[i], self.attentions[i].shape[3], prefix % (step, i), step,
                                         output_mag=False) for i in range(len(self.attentions))]
            if self.lr is not None:
                torchvision.utils.save_image(self.lr[:, :3], os.path.join(experiments_path, "attention_maps",
                                                                          "amap_%i_base_image.png" % (step,)))

    # This is a bit awkward. We want this plot to show up in TB as a histogram, but we are getting an intensity
    # plot out of the attention norm tensor. So we need to convert it back into a list of indexes, then feed into TB.
    def compute_anorm_histogram(self):
        intensities = [sw.switch.attention_norm.compute_buffer_norm().clone().detach().cpu() for sw in self.switches]
        result = []
        for intensity in intensities:
            intensity = intensity * 10
            bins = torch.tensor(list(range(len(intensity))))
            intensity = intensity.long()
            result.append(bins.repeat_interleave(intensity, 0))
        return result

    def get_debug_values(self, step, net_name):
        temp = self.switches[0].switch.temperature
        mean_hists = [compute_attention_specificity(att, 2) for att in self.attentions]
        means = [i[0] for i in mean_hists]
        hists = [i[1].clone().detach().cpu().flatten() for i in mean_hists]
        anorms = self.compute_anorm_histogram()
        val = {"switch_temperature": temp}
        for i in range(len(means)):
            val["switch_%i_specificity" % (i,)] = means[i]
            val["switch_%i_histogram" % (i,)] = hists[i]
            val["switch_%i_attention_norm_histogram" % (i,)] = anorms[i]
        return val


class ConfigurableSwitchedResidualGenerator2(nn.Module):
    def __init__(self, switch_depth, switch_filters, switch_reductions, switch_processing_layers, trans_counts, trans_kernel_sizes,
                 trans_layers, transformation_filters, attention_norm, initial_temp=20, final_temperature_step=50000, heightened_temp_min=1,
                 heightened_final_step=50000, upsample_factor=1,
                 add_scalable_noise_to_transforms=False):
        super(ConfigurableSwitchedResidualGenerator2, self).__init__()
        switches = []
        self.initial_conv = ConvBnLelu(3, transformation_filters, kernel_size=7, norm=False, activation=False, bias=True)
        self.upconv1 = ConvBnLelu(transformation_filters, transformation_filters, norm=False, bias=True)
        self.upconv2 = ConvBnLelu(transformation_filters, transformation_filters, norm=False, bias=True)
        self.hr_conv = ConvBnLelu(transformation_filters, transformation_filters, norm=False, bias=True)
        self.final_conv = ConvBnLelu(transformation_filters, 3, norm=False, activation=False, bias=True)
        for _ in range(switch_depth):
            multiplx_fn = functools.partial(ConvBasisMultiplexer, transformation_filters, switch_filters, switch_reductions, switch_processing_layers, trans_counts)
            pretransform_fn = functools.partial(ConvBnLelu, transformation_filters, transformation_filters, norm=False, bias=False, weight_init_factor=.1)
            transform_fn = functools.partial(MultiConvBlock, transformation_filters, int(transformation_filters * 1.5), transformation_filters, kernel_size=trans_kernel_sizes, depth=trans_layers, weight_init_factor=.1)
            switches.append(ConfigurableSwitchComputer(transformation_filters, multiplx_fn,
                                                       pre_transform_block=pretransform_fn, transform_block=transform_fn,
                                                       attention_norm=attention_norm,
                                                       transform_count=trans_counts, init_temp=initial_temp,
                                                       add_scalable_noise_to_transforms=add_scalable_noise_to_transforms))

        self.switches = nn.ModuleList(switches)
        self.transformation_counts = trans_counts
        self.init_temperature = initial_temp
        self.final_temperature_step = final_temperature_step
        self.heightened_temp_min = heightened_temp_min
        self.heightened_final_step = heightened_final_step
        self.attentions = None
        self.upsample_factor = upsample_factor
        self.lr = None
        assert self.upsample_factor == 2 or self.upsample_factor == 4

    def forward(self, x):
        self.lr = x.detach().cpu()

        # This is a common bug when evaluating SRG2 generators. It needs to be configured properly in eval mode. Just fail.
        if not self.train:
            assert self.switches[0].switch.temperature == 1

        x = self.initial_conv(x)

        self.attentions = []
        for i, sw in enumerate(self.switches):
            x, att = checkpoint(sw, x)
            self.attentions.append(att)

        x = self.upconv1(F.interpolate(x, scale_factor=2, mode="nearest"))
        if self.upsample_factor > 2:
            x = F.interpolate(x, scale_factor=2, mode="nearest")
        x = self.upconv2(x)
        x = self.final_conv(self.hr_conv(x))
        return x

    def set_temperature(self, temp):
        [sw.set_temperature(temp) for sw in self.switches]

    def update_for_step(self, step, experiments_path='.'):
        if self.attentions:
            temp = max(1,
                1 + self.init_temperature * (self.final_temperature_step - step) / self.final_temperature_step)
            if temp == 1 and self.heightened_final_step and step > self.final_temperature_step and \
                    self.heightened_final_step != 1:
                # Once the temperature passes (1) it enters an inverted curve to match the linear curve from above.
                # without this, the attention specificity "spikes" incredibly fast in the last few iterations.
                h_steps_total = self.heightened_final_step - self.final_temperature_step
                h_steps_current = min(step - self.final_temperature_step, h_steps_total)
                # The "gap" will represent the steps that need to be traveled as a linear function.
                h_gap = 1 / self.heightened_temp_min
                temp = h_gap * h_steps_current / h_steps_total
                # Invert temperature to represent reality on this side of the curve
                temp = 1 / temp
            self.set_temperature(temp)
            if step % 100 == 0:
                output_path = os.path.join(experiments_path, "attention_maps")
                prefix = "amap_%i_a%i_%%i.png"
                [save_attention_to_image_rgb(output_path, self.attentions[i], self.attentions[i].shape[3], prefix % (step, i), step,
                                             output_mag=False) for i in range(len(self.attentions))]
                if self.lr is not None:
                    torchvision.utils.save_image(self.lr[:, :3], os.path.join(experiments_path, "attention_maps",
                                                                              "amap_%i_base_image.png" % (step,)))

    def get_debug_values(self, step, net_name):
        temp = self.switches[0].switch.temperature
        mean_hists = [compute_attention_specificity(att, 2) for att in self.attentions]
        means = [i[0] for i in mean_hists]
        hists = [i[1].clone().detach().cpu().flatten() for i in mean_hists]
        val = {"switch_temperature": temp}
        for i in range(len(means)):
            val["switch_%i_specificity" % (i,)] = means[i]
            val["switch_%i_histogram" % (i,)] = hists[i]
        return val

@register_model
def register_ConfigurableSwitchedResidualGenerator2(opt_net, opt):
     return ConfigurableSwitchedResidualGenerator2(switch_depth=opt_net['switch_depth'],
                                                            switch_filters=opt_net['switch_filters'],
                                                            switch_reductions=opt_net['switch_reductions'],
                                                            switch_processing_layers=opt_net[
                                                                'switch_processing_layers'],
                                                            trans_counts=opt_net['trans_counts'],
                                                            trans_kernel_sizes=opt_net['trans_kernel_sizes'],
                                                            trans_layers=opt_net['trans_layers'],
                                                            transformation_filters=opt_net['transformation_filters'],
                                                            attention_norm=opt_net['attention_norm'],
                                                            initial_temp=opt_net['temperature'],
                                                            final_temperature_step=opt_net['temperature_final_step'],
                                                            heightened_temp_min=opt_net['heightened_temp_min'],
                                                            heightened_final_step=opt_net['heightened_final_step'],
                                                            upsample_factor=scale,
                                                            add_scalable_noise_to_transforms=opt_net['add_noise'],
                                                            for_video=opt_net['for_video'])