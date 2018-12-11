import tensorflow as tf
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import tensor_shape
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import rnn_cell_impl
from tensorflow.python.ops import variable_scope as vs

from video_prediction.ops import dense

import pdb


class HebbianGRUCell(rnn_cell_impl.RNNCell):
    """LSTM cell with (optional) normalization and recurrent dropout.

    The implementation is based on: tf.contrib.rnn.LayerNormBasicLSTMCell.

    It does not allow cell clipping, a projection layer, and does not
    use peep-hole connections: it is the basic baseline.
    """
    def __init__(self, input_shape, num_outputs=None, kernel_size=None,
                 forget_bias=1.0, activation_fn=math_ops.tanh,
                 normalizer_fn=None, separate_norms=True,
                 norm_gain=1.0, norm_shift=0.0,
                 dropout_keep_prob=1.0, dropout_prob_seed=None,
                 skip_connection=False, reuse=None):
        """Initializes the basic convolutional LSTM cell.

        Args:
            input_shape: int tuple, Shape of the input, excluding the batch size.
            num_outputs: int, The number of filters of the conv LSTM cell.
            kernel_size: int tuple, The kernel size of the conv LSTM cell.
            forget_bias: float, The bias added to forget gates (see above).
            activation_fn: Activation function of the inner states.
            normalizer_fn: If specified, this normalization will be applied before the
                internal nonlinearities.
            separate_norms: If set to `False`, the normalizer_fn is applied to the
                concatenated tensor that follows the convolution, i.e. before splitting
                the tensor. This case is slightly faster but it might be functionally
                different, depending on the normalizer_fn (it's functionally the same
                for instance norm but not for layer norm). Default: `True`.
            norm_gain: float, The layer normalization gain initial value. If
                `normalizer_fn` is `None`, this argument will be ignored.
            norm_shift: float, The layer normalization shift initial value. If
                `normalizer_fn` is `None`, this argument will be ignored.
            dropout_keep_prob: unit Tensor or float between 0 and 1 representing the
                recurrent dropout probability value. If float and 1.0, no dropout will
                be applied.
            dropout_prob_seed: (optional) integer, the randomness seed.
            skip_connection: If set to `True`, concatenate the input to the
                output of the conv LSTM. Default: `False`.
            reuse: (optional) Python boolean describing whether to reuse variables
                in an existing scope.  If not `True`, and the existing scope already has
                the given variables, an error is raised.
        """

        super(HebbianGRUCell, self).__init__(_reuse=reuse)

        self._input_shape = input_shape
        self._num_outputs = num_outputs
        self._kernel_size = list(kernel_size) if isinstance(kernel_size, (tuple, list)) else [kernel_size] * 2
        self._forget_bias = forget_bias
        self._activation_fn = activation_fn
        self._normalizer_fn = normalizer_fn
        self._separate_norms = separate_norms
        self._g = norm_gain
        self._b = norm_shift
        self._keep_prob = dropout_keep_prob
        self._seed = dropout_prob_seed
        self._skip_connection = skip_connection
        self._reuse = reuse

        if self._skip_connection:
            output_channels = self._num_outputs + self._input_shape[-1]
        else:
            output_channels = self._num_outputs
        cell_size = tensor_shape.TensorShape(self._input_shape)
        self._output_size = tensor_shape.TensorShape(num_outputs)

        self._state_size = [tf.TensorShape(self._num_outputs), tf.TensorShape([self._num_outputs, self._num_outputs])]

        print('done')

    @property
    def output_size(self):
        return self._output_size

    @property
    def state_size(self):
        return self._state_size


    def _norm(self, inputs, scope, bias_initializer):
        shape = inputs.get_shape()[-1:]
        gamma_init = init_ops.ones_initializer()
        beta_init = bias_initializer
        pdb.set_trace()
        with vs.variable_scope(scope):
            # Initialize beta and gamma for use by normalizer.
            vs.get_variable("gamma", shape=shape, initializer=gamma_init)
            vs.get_variable("beta", shape=shape, initializer=beta_init)
        normalized = self._normalizer_fn(inputs, reuse=True, scope=scope)
        return normalized


    def _dense(self, inputs, n_out):
        with tf.variable_scope('dense'):
            input_shape = inputs.get_shape().as_list()
            weights_shape = [input_shape[1], n_out]
            weights= tf.get_variable('weights', weights_shape, dtype=tf.float32, initializer=tf.truncated_normal_initializer(stddev=0.02))
            bias = tf.get_variable('bias', [n_out], dtype=tf.float32, initializer=tf.ones_initializer())
            # bias = tf.get_variable('bias', [n_out], dtype=tf.float32, initializer=tf.zeros_initializer())
            outputs = tf.matmul(inputs, weights) + bias
            return outputs


    def call(self, inputs, state_hebb):
        state, hebb = state_hebb

        bias_ones = init_ops.ones_initializer()
        with vs.variable_scope('gates'):
            inputs_state = array_ops.concat([inputs, state], axis=-1)
            concat = self._dense(inputs_state, self._num_outputs*2)
            if self._normalizer_fn and not self._separate_norms:
                concat = self._norm(concat, "reset_update", bias_ones)
            r_, u_ = array_ops.split(concat, 2, axis=-1)
            if self._normalizer_fn and self._separate_norms:
                r_ = self._norm(r_, "reset", bias_ones)
                u_ = self._norm(u_, "update", bias_ones)
            r_, u_ = math_ops.sigmoid(r_), math_ops.sigmoid(u_)

        bias_zeros = init_ops.zeros_initializer()
        with vs.variable_scope('candidate'):

            input_shape = inputs.get_shape()
            weights_shape = [input_shape[1], self._num_outputs]
            weights= tf.get_variable('weights_h', weights_shape, dtype=tf.float32, initializer=tf.truncated_normal_initializer(stddev=0.02))
            alpha = tf.get_variable('alpha', [input_shape[1], input_shape[1]], dtype=tf.float32, initializer=tf.truncated_normal_initializer(stddev=0.02))
            # one way of using the hebbian matrix, there might be others

            r_state = tf.expand_dims(r_ * state, 1)

            candidate = self._dense(inputs, self._num_outputs) + tf.squeeze(tf.matmul(r_state, weights[None] + hebb * alpha[None]))

            if self._normalizer_fn:
                candidate = self._norm(candidate, "state", bias_zeros)

        c_ = self._activation_fn(candidate)
        new_h = u_ * state + (1 - u_) * c_

        # hebbian update according to backpropamine
        # eta_hebb_old = tf.get_variable('eta_hebb_old', [1], dtype=tf.float32, initializer=tf.constant_initializer(value=0.01))
        # pred_eta = tf.get_variable('pred_eta',  [input_shape[1], input_shape[1]], dtype=tf.float32, initializer=tf.constant_initializer(value=0.01))
        # pred_eta_b = tf.get_variable('pred_b',  [input_shape[1]], dtype=tf.float32, initializer=tf.constant_initializer(value=0.01))
        # eta_hat = tf.sigmoid(tf.matmul(candidate[:,None], pred_eta) + pred_eta_b)

        eta = tf.get_variable('eta',  [input_shape[1]], dtype=tf.float32, initializer=tf.constant_initializer(value=0.01))

        new_hebb = (1-eta)* hebb + eta*tf.matmul(state[:,:,None], candidate[:,None])

        return new_h, new_hebb