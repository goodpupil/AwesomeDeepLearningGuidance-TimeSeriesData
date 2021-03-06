import argparse
import math
import time

import torch.nn as nn
from tools.early_stopping import EarlyStopping
from torch.optim.lr_scheduler import ReduceLROnPlateau
from models import LSTNet, CNN, baseModel, GRU_attention
from utils import *
import Optim


def evaluate(data, X, Y, model, evaluateL2, evaluateL1, batch_size):
    model.eval()
    total_loss = 0
    total_loss_l1 = 0
    n_samples = 0
    predict = None
    test = None
    
    for X, Y in data.get_batches(X, Y, batch_size, False):
        output = model(X)
        if predict is None:
            predict = output
            test = Y
        else:
            predict = torch.cat((predict,output))
            test = torch.cat((test, Y))
        
        scale = data.scale.expand(output.size(0), data.m)
        #total_loss += evaluateL2(output * scale, Y * scale).data[0]
        #total_loss_l1 += evaluateL1(output * scale, Y * scale).data[0]
        total_loss += float(evaluateL2(output * scale, Y * scale).data.item())
        total_loss_l1 += float(evaluateL1(output * scale, Y * scale).data.item())
        n_samples += (output.size(0) * data.m)
    rse = math.sqrt(total_loss / n_samples)/data.rse
    rae = (total_loss_l1/n_samples)/data.rae
    
    predict = predict.data.cpu().numpy()
    Ytest = test.data.cpu().numpy()
    sigma_p = (predict).std(axis = 0)
    sigma_g = (Ytest).std(axis = 0)
    mean_p = predict.mean(axis = 0)
    mean_g = Ytest.mean(axis = 0)
    index = (sigma_g!=0)
    correlation = ((predict - mean_p) * (Ytest - mean_g)).mean(axis = 0)/(sigma_p * sigma_g)
    correlation = (correlation[index]).mean()
    return rse, rae, correlation

def train(data, X, Y, model, criterion, optim, batch_size):
    model.train()
    total_loss = 0
    n_samples = 0
    for X, Y in data.get_batches(X, Y, batch_size, True):
        model.zero_grad()
        output = model(X)
        # 数据维度扩张
        scale = data.scale.expand(output.size(0), data.m)
        # 在这里对输出值和标签值进行了处理（每一个值乘该列对应的最大值），使其能获得数据整体层面的权重信息
        loss = criterion(output * scale, Y * scale);
        loss.backward()

        # 梯度裁剪，解决过拟合
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)
        grad_norm = optim.step()
        #total_loss += loss.data[0];
        total_loss += loss.data.item()
        n_samples += (output.size(0) * data.m)
    return total_loss / n_samples
    
parser = argparse.ArgumentParser(description='PyTorch Time series forecasting')
parser.add_argument('--data', type=str, required=True,
                    help='location of the data file')
parser.add_argument('--model', type=str, default='LSTNet',
                    help='')
parser.add_argument('--hidCNN', type=int, default=100,
                    help='number of CNN hidden units')
parser.add_argument('--hidRNN', type=int, default=100,
                    help='number of RNN hidden units')
parser.add_argument('--rnn_layers', type=int, default=1,
                    help='number of RNN hidden layers')
parser.add_argument('--window', type=int, default=24 * 7,
                    help='window size')
parser.add_argument('--CNN_kernel', type=int, default=6,
                    help='the kernel size of the CNN layers')
parser.add_argument('--highway_window', type=int, default=24,
                    help='The window size of the highway component')
parser.add_argument('--clip', type=float, default=10.,
                    help='gradient clipping')
parser.add_argument('--epochs', type=int, default=100,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=128, metavar='N',
                    help='batch size')
parser.add_argument('--dropout', type=float, default=0.2,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--seed', type=int, default=54321,
                    help='random seed')
parser.add_argument('--gpu', type=int, default=None)
parser.add_argument('--log_interval', type=int, default=2000, metavar='N',
                    help='report interval')
parser.add_argument('--save', type=str,  default='model/model.pt',
                    help='path to save the final model')
parser.add_argument('--cuda', type=str, default=True)
parser.add_argument('--optim', type=str, default='adam')
parser.add_argument('--lr', type=float, default=0.001)
parser.add_argument('--horizon', type=int, default=12)
parser.add_argument('--skip', type=float, default=24)
parser.add_argument('--hidSkip', type=int, default=5)
parser.add_argument('--L1Loss', type=bool, default=True)
parser.add_argument('--normalize', type=int, default=2)
parser.add_argument('--output_fun', type=str, default='sigmoid')
args = parser.parse_args()

args.cuda = args.gpu is not None
if args.cuda:
    torch.cuda.set_device(args.gpu)

#args.cuda = torch.cuda.is_available()
#device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")
    else:
        torch.cuda.manual_seed(args.seed)

# 数据预处理
Data = Data_utility(args.data, 0.6, 0.2, args.cuda, args.horizon, args.window, args.normalize);

# 根据命令行参数加载模型
model = eval(args.model).Model(args, Data)

if args.cuda:
    model.cuda()
    
# 计算模型参数量
nParams = sum([p.nelement() for p in model.parameters()])
print('* number of parameters: %d' % nParams)

if args.L1Loss:
    criterion = nn.L1Loss(size_average=False)
else:
    criterion = nn.MSELoss(size_average=False)
evaluateL2 = nn.MSELoss(size_average=False)
evaluateL1 = nn.L1Loss(size_average=False)
if args.cuda:
    criterion = criterion.cuda()
    evaluateL1 = evaluateL1.cuda()
    evaluateL2 = evaluateL2.cuda()
    
    
best_val = 10000000
optim = Optim.Optim(
    model.parameters(), args.optim, args.lr, args.clip,
)

# 当指标停止改进时降低学习率
# 文档：https://pytorch.apachecn.org/docs/1.2/optim.html
'''
scheduler = ReduceLROnPlateau(optim,
                              mode='min',
                              factor=0.1,            # new_lr = lr * factor
                              patience=5,            # 没有改善的周期数
                              verbose=True,          # 更新后输出信息
                              threshold=0.0001,
                              threshold_mode='rel',
                              cooldown=0,            # 减少lr后恢复正常操作之前要等待的周期数
                              min_lr=0,
                              eps=1e-08)             # 如果新旧lr之间的差异小于eps，则忽略更新
'''

early_stopping = EarlyStopping(patience=15, verbose=True)


# At any point you can hit Ctrl + C to break out of training early.
try:
    print(model)
    print('begin training')
    for epoch in range(1, args.epochs+1):
        epoch_start_time = time.time()
        train_loss = train(Data, Data.train[0], Data.train[1], model, criterion, optim, args.batch_size)
        val_loss, val_rae, val_corr = evaluate(Data, Data.valid[0], Data.valid[1], model, evaluateL2, evaluateL1, args.batch_size);
        print('| end of epoch {:3d} | time: {:5.2f}s | train_loss {:5.4f} | valid rse {:5.4f} | valid rae {:5.4f} | valid corr  {:5.4f}'.format(epoch, (time.time() - epoch_start_time), train_loss, val_loss, val_rae, val_corr))

        #scheduler.step(val_loss)

        '''
        early_stopping(val_loss, model)

        if early_stopping.early_stop:
            print("Early stopping")
            break
        '''

        # Save the model if the validation loss is the best we've seen so far.
        if val_loss < best_val:
            with open(args.save, 'wb') as f:
                torch.save(model, f)
            best_val = val_loss
        if epoch % 5 == 0:
            test_acc, test_rae, test_corr  = evaluate(Data, Data.test[0], Data.test[1], model, evaluateL2, evaluateL1, args.batch_size);
            print ("test rse {:5.4f} | test rae {:5.4f} | test corr {:5.4f}".format(test_acc, test_rae, test_corr))

except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')

# Load the best saved model.
with open(args.save, 'rb') as f:
    model = torch.load(f)
test_acc, test_rae, test_corr  = evaluate(Data, Data.test[0], Data.test[1], model, evaluateL2, evaluateL1, args.batch_size);
print('-' * 89)
print('best model')
print ("test rse {:5.4f} | test rae {:5.4f} | test corr {:5.4f}".format(test_acc, test_rae, test_corr))
