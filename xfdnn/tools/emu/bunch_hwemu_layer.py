##################################################
# Copyright 2018 Xilinx Inc.
##################################################
# The information disclosed to you hereunder (the "Materials") is provided solely for the selection and use of Xilinx products. To the
# maximum extent permitted by applicable law: (1) Materials are made available "AS IS" and with all faults, Xilinx hereby DISCLAIMS ALL
# WARRANTIES AND CONDITIONS, EXPRESS, IMPLIED, OR STATUTORY, INCLUDING BUT NOT LIMITED TO WARRANTIES OF
# MERCHANTABILITY, NON-INFRINGEMENT, OR FITNESS FOR ANY PARTICULAR PURPOSE; and (2) Xilinx shall not be liable (whether in
# contract or tort, including negligence, or under any other theory of liability) for any loss or damage of any kind or nature related to,
# arising under, or in connection with, the Materials (including your use of the Materials), including for any direct, indirect, special,
# incidental, or consequential loss or damage (including loss of data, profits, goodwill, or any type of loss or damage suffered as a result
# of any action brought by a third party) even if such damage or loss was reasonably foreseeable or Xilinx had been advised of the
# possibility of the same. Xilinx assumes no obligation to correct any errors contained in the Materials or to notify you of updates to the
# Materials or to product specifications. You may not reproduce, modify, distribute, or publicly display the Materials without prior written
# consent. Certain products are subject to the terms and conditions of Xilinx's limited warranty, please refer to Xilinx's Terms of Sale which
# can be viewed at http://www.xilinx.com/legal.htm#tos; IP cores may be subject to warranty and support terms contained in a license
# issued to you by Xilinx. Xilinx products are not designed or intended to be fail-safe or for use in any application requiring fail-safe
# performance; you assume sole risk and liability for use of Xilinx products in such critical applications, please refer to Xilinx's Terms of
# Sale which can be viewed at http://www.xilinx.com/legal.htm#tos.
##################################################

import collections
import numpy as np
import math
import os
import conv_layer
import quantize_layer as ql
import timeit

class bunch_hwemu_layer(conv_layer.conv_layer):
  def __init__(self, weights = None, stride = [1,1,1,1], 
    activation = None, padding = False, biases = 0, 
    quantize_key="", xdnn_env=None, biasv3=0) :
    super(bunch_hwemu_layer, self).__init__(weights, stride, activation, padding, biases)
    self.quantize_key = quantize_key
    self.xdnn_env = xdnn_env

  def set_params(self, layer_params, variables, 
    quantize_key="", xdnn_env=None) :
    super(bunch_hwemu_layer, self).set_params(layer_params, variables)
    self.quantize_key = quantize_key
    self.xdnn_env = xdnn_env
    return self

  def forward_exec(self,inputs) :
    if not hasattr(self, 'are_params_quantized'):
     # print self.output, self.biasv3
      self.quantize_bias()
     # print self.biasv3
      self.quantize_params()
      self.are_params_quantized = True

#    print self.output, "MNDBG layernames emu"
#    if self.output=="conv2_3x3_reduce/Conv2D_quantized":
#      print self.output, "emu", inputs[0].shape
#      dumpInp=np.copy(inputs[0])
#      dumpInp=dumpInp.flatten()
#      dumpInp=dumpInp.tolist()
#      open("xdlfEmuConv233ReduceQUantizedInputs.txt","w").close()
#      with open("xdlfEmuConv233ReduceQUantizedInputs.txt","w") as fIter:
#        for i in range(len(dumpInp)):
#          print i,dumpInp[i],"MNDBG conv_hwemu quant inps"
#          fIter.write(str(i)+" "+str(dumpInp[i])+"\n")
    inp = np.copy(inputs[0]) # assuming input is n, h, w, c
    conv_out = np.array([self.performEmuConvPlusBias(inp[0])])
    for i in range(1, inp.shape[0]) :
    	conv_out = np.concatenate(self.performEmuConvPlusBias(inp[i]), axis=0)

    return conv_out

  def performEmuConvPlusBias(self, pic):
    result = self.performConv(pic)

    # quantize inter-layer result to preserve information
    startTime = timeit.default_timer()
    xdnnParams = self.xdnn_env.get_params()

    if xdnnParams['useGlobalScale']:
      result = result / xdnnParams['scaleA']
    else:
      # transpose to group by "channel" 
      myResult = np.ascontiguousarray(\
        np.transpose(result, (2, 0, 1)), dtype=np.longlong)
      qp = xdnnParams['quantDB'][self.quantize_key]
      for ci in range(myResult.shape[0]):
        xdnnParams['api'].quantizev3InterLayer(\
          qp['prescale_shift'][ci], qp['scale'][ci], 
          qp['postscale_shift'][ci], qp['bw_params'], myResult[ci], self.biasv3[ci])
 
#    dumpList=myResult.flatten()
#    dumpList=dumpList.tolist()
#    open("xdlfEmuConv233ReduceOutputs.txt","w").close()
#    with open("xdlfEmuConv233ReduceOutputs.txt","w") as fIter:
#      for i in range(len(dumpList)):
#        fIter.write(str(i)+" "+str(dumpList[i])+'\n')
    # untranspose to restore TF order
      result = np.transpose(myResult, (1, 2, 0))
    
    elapsedTime = timeit.default_timer() - startTime
    #print "[time] quantize_inter_layer: %.2fms" % (elapsedTime*1000)

    result = self.addBias(result)
    return result

  def quantize_params(self):
    startTime = timeit.default_timer()
    xdnnParams = self.xdnn_env.get_params()
    qp = xdnnParams['quantDB'][self.quantize_key]

    if xdnnParams['useGlobalScale']:
      self.filter_weights = self.filter_weights * xdnnParams['scaleA']
    else:
      # transpose to group weight by "channel" 
      myWeights = np.ascontiguousarray(\
        np.transpose(self.filter_weights, (3,0,1,2)), dtype=np.float32)
      for c in range(myWeights.shape[0]):
        xdnnParams['api'].quantizeWeights(\
          qp['th_params'][c], qp['bw_params'], myWeights[c])
      # untranspose to restore weight to TF order
      self.filter_weights = np.transpose(myWeights, (1,2,3,0))

    # handle biases 
    if xdnnParams['useGlobalScale']:
      self.biases = self.biases * xdnnParams['scaleB']
    else:
      if isinstance(self.biases, collections.Iterable):
        f = lambda x: xdnnParams['api'].quantizeBias(\
          qp['th_layer_out'], qp['bw_params'], x)
        for x in np.nditer(self.biases, op_flags=['readwrite']):
          x[...] = f(x)
      else:
        self.biases = xdnnParams['api'].quantizeBias(\
          qp['th_layer_out'], qp['bw_params'], self.biases)

    elapsedTime = timeit.default_timer() - startTime
    #print "[time] quantize_params: %.2fms" % (elapsedTime*1000)

  def quantize_bias(self):

    xdnnParams = self.xdnn_env.get_params()
    qp = xdnnParams['quantDB'][self.quantize_key]
#    for i in range(len(self.biasv3)):
#      self.biasv3[i]=0.0
    f = lambda x: xdnnParams['api'].quantizeBias(\
      qp['th_layer_out'], qp['bw_params'], x)
    for x in np.nditer(self.biasv3, op_flags=['readwrite']):
      x[...] = f(x)
