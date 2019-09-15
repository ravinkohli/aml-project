import time
import os
import logging

import ConfigSpace as CS
import ConfigSpace.hyperparameters as CSH


import logging
import sys
sys.path.append('/home/rkohli/aml_project/src/fmin')
from fmin.entropy_search import entropy_search

from utils import get_config_dictionary, get_upper_lower, save_results_optimisation


import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.utils
import torchvision


import hpbandster.core.result as hpres
# import hpbandster.visualization as hpvis

import torchvision.transforms as transforms
from torch.autograd import Variable
from torch.utils.data.sampler import SubsetRandomSampler

from settings import get

import utils
import genotypes

from model import NetworkKMNIST as Network
from train import train, infer
from datasets import K49, KMNIST

import pickle


class ESWorker(object):
    
    def __init__(self, run_dir, experiment_no=0, min_budget=2, max_budget=5, init_channels=get('init_channels'), batch_size=get('batch_size'), split=0.8, dataset=KMNIST, **kwargs):

        super().__init__(**kwargs)
        self.init_channels = init_channels
        self.run_dir = run_dir
        data_augmentations = transforms.ToTensor()
        self.train_dataset = dataset('./data', True, data_augmentations)
        self.test_dataset = dataset('./data', False, data_augmentations)
        self.n_classes = self.train_dataset.n_classes
        self.split = split
        self.batch_size = batch_size
        if 'seed' in kwargs:
            self.seed = kwargs['seed']
        else:
            self.seed = 0
        self.experiment_no = experiment_no
        self.min_budget = min_budget
        self.max_budget = max_budget

    def compute(self, x, budget, config, **kwargs):
        """
        Get model with hyperparameters from config generated by get_configspace()
        """
        config = get_config_dictionary(x, config)
        print("config", config)
        if (len(config.keys())<len(x)):
            return 100
        if not torch.cuda.is_available():
            logging.info('no gpu device available')
            sys.exit(1)

        gpu = 'cuda:0'
        np.random.seed(self.seed)
        torch.cuda.set_device(gpu)
        cudnn.benchmark = True
        torch.manual_seed(self.seed)
        cudnn.enabled=True
        torch.cuda.manual_seed(self.seed)
        logging.info('gpu device = %s' % gpu)
        logging.info("config = %s", config)

        genotype = eval("genotypes.%s" % 'PCDARTS')
        model = Network(self.init_channels, self.n_classes, config['n_conv_layers'], genotype)
        model = model.cuda()

        logging.info("param size = %fMB", utils.count_parameters_in_MB(model))

        criterion = nn.CrossEntropyLoss()
        criterion = criterion.cuda()
        
        if config['optimizer'] == 'sgd':
            optimizer = torch.optim.SGD(model.parameters(), 
                                        lr=config['initial_lr'], 
                                        momentum=0.9, 
                                        weight_decay=config['weight_decay'], 
                                        nesterov=True)
        else:
            optimizer = get('opti_dict')[config['optimizer']](model.parameters(), lr=config['initial_lr'], weight_decay=config['weight_decay'])
        
        if config['lr_scheduler'] == 'Cosine':
            lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, int(budget))
        elif config['lr_scheduler'] == 'Exponential':
            lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.1)

        
        indices = list(range(int(self.split*len(self.train_dataset))))
        valid_indices =  list(range(int(self.split*len(self.train_dataset)), len(self.train_dataset)))
        print("Training size=", len(indices))
        training_sampler = SubsetRandomSampler(indices)
        valid_sampler = SubsetRandomSampler(valid_indices)
        train_queue = torch.utils.data.DataLoader(dataset=self.train_dataset,
                                                batch_size=self.batch_size,
                                                sampler=training_sampler) 

        valid_queue = torch.utils.data.DataLoader(dataset=self.train_dataset, 
                                                batch_size=self.batch_size, 
                                                sampler=valid_sampler)


        for epoch in range(int(budget)):
            lr_scheduler.step()
            logging.info('epoch %d lr %e', epoch, lr_scheduler.get_lr()[0])
            model.drop_path_prob = config['drop_path_prob'] * epoch / int(budget)

            train_acc, train_obj = train(train_queue, model, criterion, optimizer, grad_clip=config['grad_clip_value'])
            logging.info('train_acc %f', train_acc)

            valid_acc, valid_obj = infer(valid_queue, model, criterion)
            logging.info('valid_acc %f', valid_acc)

        return valid_obj # Hyperband always minimizes, so we want to minimise the error, error = 1-acc
    
    @staticmethod
    def get_configspace():
        """
        Define all the hyperparameters that need to be optimised and store them in config
        """
        cs = CS.ConfigurationSpace()
        n_conv_layers = CSH.UniformIntegerHyperparameter('n_conv_layers', lower=3, upper=6)        
        initial_lr = CSH.UniformFloatHyperparameter('initial_lr', lower=1e-6, upper=1e-1, default_value='1e-2', log=True)
        optimizer = CSH.CategoricalHyperparameter('optimizer', get['opti_dict'].keys())
        cs.add_hyperparameters([initial_lr, optimizer, n_conv_layers])
        
        lr_scheduler = CSH.CategoricalHyperparameter('lr_scheduler', ['Exponential', 'Cosine'])
        weight_decay = CSH.UniformFloatHyperparameter('weight_decay', lower=1e-5, upper=1e-3, default_value=3e-4, log=True)
        drop_path_prob = CSH.UniformFloatHyperparameter('drop_path_prob', lower=0, upper=0.4, default_value=0.3, log=False)
        grad_clip_value = CSH.UniformIntegerHyperparameter('grad_clip_value', lower=4, upper=8, default_value=5)
        cs.add_hyperparameters([lr_scheduler, drop_path_prob, weight_decay, grad_clip_value])
        return cs


    def run_es(self, iterations=20):
        cs = self.__class__.get_configspace()
        
        lower, upper = get_upper_lower(cs)
        if not os.path.exists(self.run_dir):
            os.mkdir(self.run_dir)
        log_dir = os.path.join(self.run_dir, f'EXP{self.experiment_no}')
        if not os.path.exists(log_dir):
            os.mkdir(log_dir)
        
        results = entropy_search(self.compute, lower, upper, num_iterations=iterations, cs=cs, min_budget=self.min_budget, max_budget=self.max_budget)
        save_results_optimisation(results, log_dir)
        x_best = results["x_opt"]
        self.experiment_no += 1
        print(x_best)

if __name__ =='__main__':
    worker = ESWorker('./es', experiment_no=1)
    worker.run_es()