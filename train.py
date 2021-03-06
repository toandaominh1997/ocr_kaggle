import subprocess
import os
import argparse
import random
import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.optim as optim
from torch.autograd import Variable
from torch.nn import CTCLoss
import time
import datetime
from tqdm import tqdm

from dataset import dataset
from dataset import aug

from models import model
from models import dcrnn

from util import util
from util import convert
from util import metric

parser = argparse.ArgumentParser()

parser.add_argument('--root', default='/opt/ml/input/data/train', help='path to dataset')
parser.add_argument('--train_label', default='train_final_combine_normalize_withspace_v2.txt', help='path to dataset')
parser.add_argument('--valid_label', default= 'val_final_combine_normalize_withspace_v2.txt', help='path to dataset')
parser.add_argument('--test_label', default=None, help='path to dataset')
parser.add_argument('--num_worker', type=int, help='number of data loading workers', default=10)
parser.add_argument('--batch_size', type=int, default=24, help='input batch size')
parser.add_argument('--hidden_size', type=int, default=256, help='input hidden size')
parser.add_argument('--height', type=int, default=48, help='the height of the input image to network')
parser.add_argument('--alphabet', type=str, default='0123456789abcdefghijklmnopqrstuvwxyz')
parser.add_argument('--num_class', type=int, default=48, help='the number class of the input image to network')
parser.add_argument('--num_epoch', type=int, default=50, help='number of epochs to train for')
parser.add_argument('--learning_rate', type=float, default=0.0001, help='learning rate for neural network')
parser.add_argument('--cuda', default=True, help='enables cuda')
parser.add_argument('--num_gpu', type=int, default=1, help='number of GPUs to use')
parser.add_argument('--display', type=int, default=10000, help='display iteration each epoch')
parser.add_argument('--resume', default=None, help="path to pretrained model (to continue training)")
parser.add_argument('--save_dir', default='/opt/ml/model/', help='Where to store samples and models')
parser.add_argument('--manual_seed', type=int, default=1234, help='reproduce experiemnt')
parser.add_argument('--net', default='densenet', help='Choose model in training')

args = parser.parse_args()

start_time = datetime.datetime.now().strftime('%m-%d_%H%M%S')
if(not os.path.exists(os.path.join(args.save_dir, start_time))):
    os.makedirs(os.path.join(args.save_dir, start_time))

random.seed(args.manual_seed)
np.random.seed(args.manual_seed)
torch.manual_seed(args.manual_seed)
cudnn.benchmark = False

if(torch.cuda.is_available() and args.cuda):
    torch.cuda.set_device(util.get_gpu())
if(torch.cuda.is_available() and not args.cuda):
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")

train_transform = aug.train_transforms(height=args.height)
test_transform = aug.test_transforms(height=args.height)

train_dataset = dataset.ocrDataset(args=args, root=args.root, label=args.train_label, training=True, transform=train_transform)
valid_dataset = dataset.ocrDataset(args=args, root=args.root, label=args.valid_label, training=False, transform=test_transform)

train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=int(args.num_worker), collate_fn=dataset.alignCollate())
valid_loader = torch.utils.data.DataLoader(valid_dataset, batch_size=args.batch_size, shuffle=False, num_workers=int(args.num_worker), collate_fn=dataset.alignCollate())

if(args.test_label is not None):
    test_dataset = dataset.ocrDataset(args=args, root=args.root, label=args.test_label, train=False, transform=test_transform)
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=int(args.num_worker), collate_fn=dataset.alignCollate())

if args.resume is not None:
    print('loading pretrained class from {}'.format(args.resume))
    checkpoint = torch.load(args.resume, map_location=lambda storage, loc: storage)
    args.alphabet = checkpoint['alphabet']
    del checkpoint
else:
    args.alphabet = util.get_vocab(root=args.root, label=args.train_label)

args.num_class = len(args.alphabet) + 1
converter = convert.strLabelConverter(args.alphabet)

model = model.Model(num_classes=args.num_class, fixed_height=args.height, net=args.net)
model = dcrnn.Model(n_classes=args.num_class, fixed_height=args.height)
optimizer = optim.Adam(model.parameters(), lr=args.learning_rate, betas=(0.5, 0.999))

if args.resume is not None:
    print('loading pretrained model from {}'.format(args.resume))
    checkpoint = torch.load(args.resume, map_location=lambda storage, loc: storage)
    model.load_state_dict(checkpoint['state_dict'])
    del checkpoint
criterion = CTCLoss()

global image, text, length
image = torch.FloatTensor(args.batch_size, 1, args.height, 500)
text = torch.LongTensor(args.batch_size * 10)
length = torch.LongTensor(args.batch_size)

if(torch.cuda.is_available() and args.cuda):
    model = model.cuda()
    image = image.cuda()
    text = text.cuda()
    criterion = criterion.cuda()

image = Variable(image)
text = Variable(text)
length = Variable(length)

def train(data_loader):
    total_loss=0
    model.train()
    data_loader = tqdm(data_loader)
    for idx, (cpu_images, cpu_texts) in enumerate(data_loader):
        batch_size = cpu_images.size(0)
        util.loadData(image, cpu_images)
        t, l = converter.encode(cpu_texts)
        util.loadData(text, t)
        util.loadData(length, l)
        output = model(image)
        output_size = Variable(torch.LongTensor([output.size(0)] * batch_size))
        loss = criterion(output, text, output_size, length)/batch_size
        optimizer.zero_grad()
        loss.backward()
        clipping_value = 1.0
        torch.nn.utils.clip_grad_norm_(model.parameters(), clipping_value)
        if not (torch.isnan(loss) or torch.isinf(loss)):
            optimizer.step()
        total_loss+=loss.item()
        if idx%1000==0 and idx!=0 :
            print('{} index: {}/{}(~{}%) loss: {}'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), idx, len(data_loader), round(idx*100/len(data_loader)), total_loss/idx))
    return total_loss/len(data_loader)

def evaluate(data_loader):
    model.eval()
    total_loss=0
    accBF = 0.0
    accBC = 0.0
    with torch.no_grad():
        for idx, (cpu_images, cpu_texts) in enumerate(data_loader):
            batch_size = cpu_images.size(0)
            util.loadData(image, cpu_images)
            t, l = converter.encode(cpu_texts)
            util.loadData(text, t)
            util.loadData(length, l)
            output = model(image)
            output_size = Variable(torch.LongTensor([output.size(0)] * batch_size))
            loss = criterion(output, text, output_size, length)/batch_size
            total_loss+=loss
            _, output = output.max(2)
            output = output.transpose(1, 0).contiguous().view(-1)
            sim_preds = converter.decode(output.data, output_size.data, raw=False)
            accBF += metric.by_field(sim_preds, cpu_texts)
            accBC += metric.by_char(sim_preds, cpu_texts)
        total_loss /= len(data_loader)
        return total_loss, accBF/len(data_loader), accBC/len(data_loader)

def main():
    by_field_best = 0.0
    for epoch in range(1, args.num_epoch):
        model_best = False
        loss = train(train_loader)
        val_loss, val_by_field, val_by_char = evaluate(valid_loader)
        if val_by_field>by_field_best:
            model_best = True
        log = {'epoch': epoch}
        log['loss'] = loss
        log['val_loss'] = val_loss
        log['val_by_field'] = val_by_field
        log['val_by_char'] = val_by_char
        if(args.test_label is not None):
            test_loss, test_by_field, test_by_char = evaluate(test_loader)
            log['test_loss'] = test_loss
            log['test_by_field'] = test_by_field
            log['test_by_char'] = test_by_char
        for key, value in log.items():
            print('    {:15s}: {}'.format(str(key), value))
        util.save_checkpoint(args, epoch, model, args.save_dir, model_best, start_time)

if __name__ == '__main__':
    main()
