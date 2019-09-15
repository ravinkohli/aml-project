import pickle
import datetime
import os
import logging
import time
import glob


import ConfigSpace as CS
import ConfigSpace.hyperparameters as CSH
from hpbandster.core.worker import Worker
import hpbandster.core.result as hpres
# import hpbandster.visualization as hpvis
import hpbandster.core.nameserver as hpns
from hpbandster.optimizers import BOHB

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.utils
import torchvision

import torchvision.transforms as transforms
from torch.autograd import Variable
from torch.utils.data.sampler import SubsetRandomSampler


import sys
import settings

import utils
import genotypes

from model import NetworkKMNIST as Network
from train import train, infer
from datasets import K49, KMNIST



class TorchWorker(Worker):
    def __init__(self, run_dir,init_channels=16, batch_size=64, split=0.8, dataset=KMNIST, **kwargs):

        super().__init__(**kwargs)
        self.init_channels = init_channels
        self.run_dir = log_dir
        data_augmentations = transforms.ToTensor()
        self.train_dataset = dataset('./data', True, data_augmentations)
        self.test_dataset = dataset('./data', False, data_augmentations)
        self.n_classes = self.train_dataset.n_classes
        self.split = split
        self.batch_size = batch_size


    def compute(self, config, budget, *args, **kwargs):
        """
        Get model with hyperparameters from config generated by get_configspace()
        """
        if not torch.cuda.is_available():
            logging.info('no gpu device available')
            sys.exit(1)

        gpu = 'cuda:0'
        np.random.seed(config.seed)
        torch.cuda.set_device(gpu)
        cudnn.benchmark = True
        torch.manual_seed(config.seed)
        cudnn.enabled=True
        torch.cuda.manual_seed(config.seed)
        logging.info('gpu device = %d' % gpu)
        logging.info("config = %s", config)

        genotype = eval("genotypes.%s" % 'PCDARTS')
        model = Network(self.init_channels, self.n_classes, config.n_conv_layers, genotype)
        model = model.cuda()

        logging.info("param size = %fMB", utils.count_parameters_in_MB(model))

        criterion = nn.CrossEntropyLoss()
        criterion = criterion.cuda()
        
        if config['optimizer'] == 'sgd':
            optimizer = torch.optim.SGD(model.parameters(), 
                                        lr=config['initial_lr'], 
                                        momentum=config['sgd_momentum'], 
                                        weight_decay=config['weight_decay'], 
                                        nesterov=config['nesterov'])
        else:
            optimizer = settings.opti_dict[config['optimizer']](model.parameters(), lr=config['initial_lr'])
        
        if config['lr_schedular'] == 'Cosine':
            lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, int(budget))
        elif config['lr_schedular'] == 'Exponential':
            lr_schedular = torch.optim.lr_schedular.ExponnetialLR(optimizer, gamma=0.1)
        elif config['lr_schedular'] == 'Plateau':
            lr_schedular = torch.optim.lr_schedular.ReduceLROnPlateau(optimizer)
        
        indices = list(range(int(self.split*len(self.train_dataset))))
        valid_indices =  list(range(int(self.split*len(self.train_dataset)),len(self.train_dataset)))
        print("Training size=", len(indices))
        training_sampler = SubsetRandomSampler(indices)
        valid_sampler = SubsetRandomSampler(valid_indices)
        train_queue = torch.utils.data.DataLoader(dataset=self.train_dataset,
                              batch_size=self.batch_size,
                              sampler=training_sampler, shuffle=True) 

        valid_queue = torch.utils.data.DataLoader(dataset=self.train_dataset, 
                                                batch_size=self.batch_size, 
                                                shuffle=False, sampler=valid_sampler )


        for epoch in range(int(budget)):
            lr_scheduler.step()
            logging.info('epoch %d lr %e', epoch, lr_scheduler.get_lr()[0])
            model.drop_path_prob = config.drop_path_prob * epoch / int(budget)

            train_acc, train_obj = train(train_queue, model, criterion, optimizer, grad_clip=config.grad_clip_value)
            logging.info('train_acc %f', train_acc)

            valid_acc, valid_obj = infer(valid_queue, model, criterion)
            logging.info('valid_acc %f', valid_acc)

        return({
            'loss': valid_obj,  # Hyperband always minimizes, so we want to minimise the error, error = 1-accuracy
            'info': {}  # mandatory- can be used in the future to give more information
        })

        
    @staticmethod
    def get_configspace():
        """
        Define all the hyperparameters that need to be optimised and store them in config
        """
        cs = CS.ConfigurationSpace()
        n_conv_layers = CSH.UniformIntegerHyperparameter('n_conv_layers', lower=3, upper=6)        
        initial_lr = CSH.UniformFloatHyperparameter('initial_lr', lower=1e-6, upper=1e-1, default_value='1e-2', log=True)
        optimizer = CSH.CategoricalHyperparameter('optimizer', settings.opti_dict.keys())
        sgd_momentum = CSH.UniformFloatHyperparameter('sgd_momentum', lower=0.0, upper=0.99, default_value=0.9, log=False)
        nesterov = CSH.CategoricalHyperparameter('nesterov', ['True', 'False'])
        cs.add_hyperparameters([initial_lr, optimizer, sgd_momentum, nesterov, n_conv_layers])
        
        lr_scheduler = CSH.CategoricalHyperparameter('scheduler', ['Exponential', 'Cosine', 'Plateau'])
        weight_decay = CSH.UniformFloatHyperparameter('weight_decay', lower=1e-5, upper=1e-3, default_value=3e-4, log=True)
        drop_path_prob = CSH.UniformFloatHyperparameter('drop_path_prob', lower=0, upper=0.4, default_value=0.3, log=False)
        grad_clip_value = CSH.UniformIntegerHyperparameter('grad_clip_value', lower=4, upper=8, default_value=5)
        cs.add_hyperparameters([lr_scheduler, drop_path_prob, weight_decay, grad_clip_value])

        cond = CS.EqualsCondition(sgd_momentum, optimizer, 'sgd')
        cs.add_condition(cond)
        cond2 = CS.EqualsCondition(nesterov, optimizer, 'sgd')
        cs.add_condition(cond2)

        return cs


def run_bohb(exp_name, log_dir='EXP', iterations=20):
    
    run_dir = 'bohb-{}-{}'.format(log_dir, exp_name)
    utils.create_exp_dir(run_dir, scripts_to_save=glob.glob('*.py'))

    log_format = '%(asctime)s %(message)s'
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
        format=log_format, datefmt='%m/%d %I:%M:%S %p')
    fh = logging.FileHandler(os.path.join(run_dir, 'log.txt'))
    fh.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(fh)

    result_logger = hpres.json_result_logger(directory=run_dir, overwrite=True)

    # Start a nameserver
    NS = hpns.NameServer(run_id=exp_name, host='127.0.0.1', port=0)
    ns_host, ns_port = NS.start()

    # Start a localserver
    worker = TorchWorker(run_id=exp_name, host='127.0.0.1', nameserver=ns_host, nameserver_port=ns_port,
                        timeout=120, run_dir=run_dir)
    worker.run(background=True)

    # Initialise optimiser
    bohb = BOHB(configspace=worker.get_configspace(),
                run_id=exp_name,
                host='127.0.0.1',
                nameserver=ns_host, 
                nameserver_port=ns_port,
                result_logger=result_logger,
                min_budget=2, max_budget=5,
                )
    res = bohb.run(n_iterations=iterations)

    # Store the results
    with open(os.path.join(run_dir, 'result.pkl'), 'wb') as file:
        pickle.dump(res, file)
    
    # Shutdown
    bohb.shutdown(shutdown_workers=True)
    NS.shutdown()

    # get all runs
    all_runs = res.get_all_runs()

    # get id to configuration mapping as dictionary
    id2conf = res.get_id2config_mapping()

    # get best/incubent run
    best_run = res.get_incumbent_id()
    best_config = id2conf[best_run]['config']
    
    print(f"Best run id:{best_run}, \n Config:{best_config}")

    # Store all run info
    file = open(os.path.join(run_dir, 'summary.txt'), 'w')
    file.write(f"{all_runs}")
    file.close()

run_bohb('01', 20)