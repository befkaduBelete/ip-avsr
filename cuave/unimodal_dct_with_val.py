from __future__ import print_function
import sys
sys.path.insert(0, '../')
import pickle
import time
import ConfigParser
import argparse

import matplotlib
matplotlib.use('Agg')  # Change matplotlib backend, in case we have no X server running..

from utils.preprocessing import *
from utils.plotting_utils import *
from utils.data_structures import circular_list
from utils.datagen import *
from utils.io import *

import theano.tensor as T
import theano
from custom_layers.custom import DeltaLayer
from nolearn.lasagne import NeuralNet

import lasagne as las
import numpy as np
from lasagne.layers import InputLayer, DenseLayer, DropoutLayer, LSTMLayer, Gate, ElemwiseSumLayer, SliceLayer
from lasagne.layers import ReshapeLayer, DimshuffleLayer, ConcatLayer
from lasagne.nonlinearities import tanh, linear, sigmoid, rectify
from lasagne.updates import nesterov_momentum, adadelta, sgd, norm_constraint, adagrad
from lasagne.objectives import squared_error

from modelzoo import lstm_classifier_baseline
from utils.plotting_utils import print_network


def load_dbn(path='models/cuave_ae.mat'):
    """
    load a pretrained dbn from path
    :param path: path to the .mat dbn
    :return: pretrained deep belief network
    """
    # create the network using weights from pretrain_nn.mat
    nn = sio.loadmat(path)
    w1 = nn['w1']
    w2 = nn['w2']
    w3 = nn['w3']
    w4 = nn['w4']
    w5 = nn['w5']
    w6 = nn['w6']
    w7 = nn['w7']
    w8 = nn['w8']
    b1 = nn['b1'][0]
    b2 = nn['b2'][0]
    b3 = nn['b3'][0]
    b4 = nn['b4'][0]
    b5 = nn['b5'][0]
    b6 = nn['b6'][0]
    b7 = nn['b7'][0]
    b8 = nn['b8'][0]

    layers = [
        (InputLayer, {'name': 'input', 'shape': (None, 1500)}),
        (DenseLayer, {'name': 'l1', 'num_units': 2000, 'nonlinearity': sigmoid, 'W': w1, 'b': b1}),
        (DenseLayer, {'name': 'l2', 'num_units': 1000, 'nonlinearity': sigmoid, 'W': w2, 'b': b2}),
        (DenseLayer, {'name': 'l3', 'num_units': 500, 'nonlinearity': sigmoid, 'W': w3, 'b': b3}),
        (DenseLayer, {'name': 'l4', 'num_units': 50, 'nonlinearity': linear, 'W': w4, 'b': b4}),
        (DenseLayer, {'name': 'l5', 'num_units': 500, 'nonlinearity': sigmoid, 'W': w5, 'b': b5}),
        (DenseLayer, {'name': 'l6', 'num_units': 1000, 'nonlinearity': sigmoid, 'W': w6, 'b': b6}),
        (DenseLayer, {'name': 'l7', 'num_units': 2000, 'nonlinearity': sigmoid, 'W': w7, 'b': b7}),
        (DenseLayer, {'name': 'output', 'num_units': 1144, 'nonlinearity': linear, 'W': w8, 'b': b8}),
    ]

    dbn = NeuralNet(
        layers=layers,
        max_epochs=30,
        objective_loss_function=squared_error,
        update=nesterov_momentum,
        regression=True,
        verbose=1,
        update_learning_rate=0.001,
        update_momentum=0.05,
        objective_l2=0.005,
    )
    return dbn


def extract_encoder(dbn):
    dbn_layers = dbn.get_all_layers()
    encoder = NeuralNet(
        layers=[
            (InputLayer, {'name': 'input', 'shape': dbn_layers[0].shape}),
            (DenseLayer, {'name': 'l1', 'num_units': dbn_layers[1].num_units, 'nonlinearity': sigmoid,
                          'W': dbn_layers[1].W, 'b': dbn_layers[1].b}),
            (DenseLayer, {'name': 'l2', 'num_units': dbn_layers[2].num_units, 'nonlinearity': sigmoid,
                          'W': dbn_layers[2].W, 'b': dbn_layers[2].b}),
            (DenseLayer, {'name': 'l3', 'num_units': dbn_layers[3].num_units, 'nonlinearity': sigmoid,
                          'W': dbn_layers[3].W, 'b': dbn_layers[3].b}),
            (DenseLayer, {'name': 'l4', 'num_units': dbn_layers[4].num_units, 'nonlinearity': linear,
                          'W': dbn_layers[4].W, 'b': dbn_layers[4].b}),
        ],
        update=nesterov_momentum,
        update_learning_rate=0.001,
        update_momentum=0.5,
        objective_l2=0.005,
        verbose=1,
        regression=True
    )
    encoder.initialize()
    return encoder


def configure_theano():
    theano.config.floatX = 'float32'
    sys.setrecursionlimit(10000)


def read_data_split_file(path, sep=','):
    with open(path) as f:
        subjects = f.readline().split(sep)
        subjects = [int(s) for s in subjects]
    return subjects


def create_pretrained_encoder(weights, biases, incoming):
    l_1 = DenseLayer(incoming, 2000, W=weights[0], b=biases[0], nonlinearity=sigmoid, name='fc1')
    l_2 = DenseLayer(l_1, 1000, W=weights[1], b=biases[1], nonlinearity=sigmoid, name='fc2')
    l_3 = DenseLayer(l_2, 500, W=weights[2], b=biases[2], nonlinearity=sigmoid, name='fc3')
    l_4 = DenseLayer(l_3, 50, W=weights[3], b=biases[3], nonlinearity=linear, name='encoder')
    return l_4


def evaluate_model(X_val, y_val, mask_val, eval_fn):
    """
    Evaluate a lstm model
    :param X_val: validation inputs
    :param y_val: validation targets
    :param mask_val: input masks for variable sequences
    :param eval_fn: evaluation function
    :return: classification rate, confusion matrix
    """
    output = eval_fn(X_val, mask_val)
    no_gps = output.shape[1]
    confusion_matrix = np.zeros((no_gps, no_gps), dtype='int')

    ix = np.argmax(output, axis=1)
    c = ix == y_val
    classification_rate = np.sum(c == True) / float(len(c))

    # construct the confusion matrix
    for i, target in enumerate(y_val):
        confusion_matrix[target, ix[i]] += 1

    return classification_rate, confusion_matrix


def parse_options():
    options = dict()
    options['config'] = 'config/unimodal_dct.ini'
    options['write_results'] = ''
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='config file to use, default=config/unimodal_dct.ini')
    parser.add_argument('--write_results', help='write results to file')
    args = parser.parse_args()
    if args.config:
        options['config'] = args.config
    if args.write_results:
        options['write_results'] = args.write_results
    return options


def main():
    configure_theano()
    options = parse_options()
    config_file = options['config']
    config = ConfigParser.ConfigParser()
    config.read(config_file)

    print('CLI options: {}'.format(options.items()))

    print('Reading Config File: {}...'.format(config_file))
    print(config.items('data'))
    print(config.items('models'))
    print(config.items('training'))

    print('preprocessing dataset...')
    data = load_mat_file(config.get('data', 'images'))
    dct_data = load_mat_file(config.get('data', 'dct'))
    fusiontype = config.get('models', 'fusiontype')
    learning_rate = float(config.get('training', 'learning_rate'))
    decay_rate = float(config.get('training', 'decay_rate'))
    decay_start = int(config.get('training', 'decay_start'))

    train_vidlens = data['trVideoLengthVec'].astype('int').reshape((-1,))
    val_vidlens = data['valVideoLengthVec'].astype('int').reshape((-1,))
    test_vidlens = data['testVideoLengthVec'].astype('int').reshape((-1,))
    train_X = data['trData'].astype('float32')
    val_X = data['valData'].astype('float32')
    test_X = data['testData'].astype('float32')
    train_dct = dct_data['trDctFeatures'].astype('float32')
    val_dct = dct_data['valDctFeatures'].astype('float32')
    test_dct = dct_data['testDctFeatures'].astype('float32')
    # +1 to handle the -1 introduced in lstm_gendata
    train_y = data['trTargetsVec'].astype('int').reshape((-1,)) + 1
    val_y = data['valTargetsVec'].astype('int').reshape((-1,)) + 1
    test_y = data['testTargetsVec'].astype('int').reshape((-1,)) + 1

    # featurewise normalize dct features
    train_dct, dct_mean, dct_std = featurewise_normalize_sequence(train_dct)
    val_dct = (val_dct - dct_mean) / dct_std
    test_dct = (test_dct - dct_mean) / dct_std

    # IMPT: the encoder was trained with fortan ordered images, so to visualize
    # convert all the images to C order using reshape_images_order()
    # output = dbn.predict(test_X)
    # test_X = reshape_images_order(test_X, (26, 44))
    # output = reshape_images_order(output, (26, 44))
    # visualize_reconstruction(test_X[:36, :], output[:36, :], shape=(26, 44))

    inputs = T.tensor3('inputs', dtype='float32')
    mask = T.matrix('mask', dtype='uint8')
    targets = T.ivector('targets')
    lr = theano.shared(np.array(learning_rate, dtype=theano.config.floatX), name='learning_rate')
    lr_decay = np.array(decay_rate, dtype=theano.config.floatX)

    print('constructing end to end model...')
    network = lstm_classifier_baseline.create_model((None, None, 90), inputs,
                                                    (None, None), mask,
                                                    250, 10)

    print_network(network)
    print('compiling model...')
    predictions = las.layers.get_output(network, deterministic=False)
    all_params = las.layers.get_all_params(network, trainable=True)
    cost = T.mean(las.objectives.categorical_crossentropy(predictions, targets))
    updates = adadelta(cost, all_params, learning_rate=lr)

    train = theano.function(
        [inputs, targets, mask],
        cost, updates=updates, allow_input_downcast=True)
    compute_train_cost = theano.function([inputs, targets, mask], cost, allow_input_downcast=True)

    test_predictions = las.layers.get_output(network, deterministic=True)
    test_cost = T.mean(las.objectives.categorical_crossentropy(test_predictions, targets))
    compute_test_cost = theano.function(
        [inputs, targets, mask], test_cost, allow_input_downcast=True)

    val_fn = theano.function([inputs, mask], test_predictions, allow_input_downcast=True)

    # We'll train the network with 10 epochs of 30 minibatches each
    print('begin training...')
    cost_train = []
    cost_val = []
    class_rate = []
    NUM_EPOCHS = 10
    EPOCH_SIZE = 45
    BATCH_SIZE = 20
    WINDOW_SIZE = 9
    STRIP_SIZE = 3
    MAX_LOSS = 0.2
    VALIDATION_WINDOW = 4
    val_window = circular_list(VALIDATION_WINDOW)
    train_strip = np.zeros((STRIP_SIZE,))
    best_val = float('inf')
    best_conf = None
    best_cr = 0.0

    datagen = gen_lstm_batch_random(train_X, train_y, train_vidlens, batchsize=BATCH_SIZE)
    val_datagen = gen_lstm_batch_random(val_X, val_y, val_vidlens, batchsize=len(val_vidlens))
    test_datagen = gen_lstm_batch_random(test_X, test_y, test_vidlens, batchsize=len(test_vidlens))
    integral_lens = compute_integral_len(train_vidlens)

    # We'll use this "validation set" to periodically check progress
    X_val, y_val, mask_val, idxs_val = next(val_datagen)
    integral_lens_val = compute_integral_len(val_vidlens)
    dct_val = gen_seq_batch_from_idx(val_dct, idxs_val, val_vidlens, integral_lens_val, np.max(val_vidlens))

    X_test, y_test, mask_test, idxs_test = next(test_datagen)
    integral_lens_test = compute_integral_len(test_vidlens)
    dct_test = gen_seq_batch_from_idx(test_dct, idxs_test, test_vidlens, integral_lens_test, np.max(test_vidlens))

    def early_stop(cost_window):
        if len(cost_window) < 2:
            return False
        else:
            curr = cost_window[0]
            for idx, cost in enumerate(cost_window):
                if curr < cost or idx == 0:
                    curr = cost
                else:
                    return False
            return True

    for epoch in range(NUM_EPOCHS):
        time_start = time.time()
        for i in range(EPOCH_SIZE):
            X, y, m, batch_idxs = next(datagen)
            d = gen_seq_batch_from_idx(train_dct, batch_idxs,
                                       train_vidlens, integral_lens, np.max(train_vidlens))
            print_str = 'Epoch {} batch {}/{}: {} examples at learning rate = {:.4f}'.format(
                epoch + 1, i + 1, EPOCH_SIZE, len(X), float(lr.get_value()))
            print(print_str, end='')
            sys.stdout.flush()
            train(d, y, m)
            print('\r', end='')
        cost = compute_train_cost(d, y, m)
        val_cost = compute_test_cost(dct_val, y_val, mask_val)
        cost_train.append(cost)
        cost_val.append(val_cost)
        train_strip[epoch % STRIP_SIZE] = cost
        val_window.push(val_cost)

        gl = 100 * (cost_val[-1] / np.min(cost_val) - 1)
        pk = 1000 * (np.sum(train_strip) / (STRIP_SIZE * np.min(train_strip)) - 1)
        pq = gl / pk

        cr, val_conf = evaluate_model(dct_val, y_val, mask_val, val_fn)
        class_rate.append(cr)

        if val_cost < best_val:
            best_val = val_cost
            best_conf = val_conf
            best_cr = cr
            test_cr, test_conf = evaluate_model(dct_test, y_test, mask_test, val_fn)
            print("Epoch {} train cost = {}, val cost = {}, "
                  "GL loss = {:.3f}, GQ = {:.3f}, CR = {:.3f}, Test CR= {:.3f} ({:.1f}sec)"
                  .format(epoch + 1, cost_train[-1], cost_val[-1], gl, pq, cr, test_cr, time.time() - time_start))
        else:
            print("Epoch {} train cost = {}, val cost = {}, "
                  "GL loss = {:.3f}, GQ = {:.3f}, CR = {:.3f} ({:.1f}sec)"
                  .format(epoch + 1, cost_train[-1], cost_val[-1], gl, pq, cr, time.time() - time_start))

        if epoch >= VALIDATION_WINDOW and early_stop(val_window):
            break

        # learning rate decay
        if epoch + 1 >= decay_start:
            lr.set_value(lr.get_value() * lr_decay)

    numbers = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9']

    print('Final Model')
    print('CR: {}, val loss: {}, Test CR: {}'.format(best_cr, best_val, test_cr))
    print('confusion matrix: ')
    plot_confusion_matrix(test_conf, numbers, fmt='latex')
    plot_validation_cost(cost_train, cost_val, savefilename='valid_cost')



    if options['write_results']:
        results_file = options['write_results']
        with open(results_file, mode='a') as f:
            f.write('{},{},{}\n'.format(fusiontype, test_cr, best_val))


if __name__ == '__main__':
    main()
