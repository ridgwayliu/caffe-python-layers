import caffe
import sys
import argparse, pprint
import time
import numpy as np
import scipy.misc as scm
from easydict import EasyDict as edict
from multiprocessing import Pool
from collections import deque
from pycaffe_config import cfg
from os import path as osp
sys.path.append(osp.join(cfg.BILLIARDS_CODE_PATH, 'code/physicsEngine'))
import dataio as dio
import pdb
import Queue
import glog
import matplotlib.pyplot as plt
from matplotlib import cm as cmap

def wrapper_fetch_data(args):
	rndSeed, params = args
	params['randSeed'] = rndSeed
	ds  = dio.DataSaver(**params)	
	ims = ds.fetch(params['glimpseSz'], params['imSz'])
	#imNew = []
	#for imBalls, f, p in ims:
	#	imB = []
	#	for im in imBalls:
	#		im = im.transpose(axes=(2,0,1))
	#		imB.append(im)
	#	imNew.append(im, f, p)
	#return imNew
	#print ('LENGTH IN WRAPPER', len(ims))
	return ims

class DataFetchLayer(caffe.Layer):
	@classmethod
	def parse_args(cls, argsStr):
		parser = argparse.ArgumentParser(description='Try Layer')
		parser.add_argument('--numBalls', default=1, type=int)
		parser.add_argument('--oppForce',    dest='oppForce', action='store_true')
		parser.add_argument('--no-oppForce', dest='oppForce', action='store_false')
		parser.add_argument('--mnBallSz', default=15, type=int)
		parser.add_argument('--mxBallSz', default=35, type=int)
		parser.add_argument('--mnSeqLen', default=10, type=int)
		parser.add_argument('--mxSeqLen', default=100, type=int)
		parser.add_argument('--mnForce',  default=1e+3, type=float)
		parser.add_argument('--mxForce',  default=1e+5, type=float)
		parser.add_argument('--isRect', dest='isRect', action='store_true', default=True)
		parser.add_argument('--no-isRect', dest='isRect', action='store_false')
		parser.add_argument('--whiteMean',    dest='whiteMean', action='store_true', default=False)
		parser.add_argument('--no-whiteMean', dest='whiteMean', action='store_false')
		parser.add_argument('--scale',  default=1.0, type=float)
		parser.add_argument('--wTheta' ,  default=30,   type=float)
		parser.add_argument('--wThick' ,  default=30,   type=float)
		parser.add_argument('--mxWLen' ,  default=600, type=int)
		parser.add_argument('--mnWLen' ,  default=200, type=int)
		parser.add_argument('--randSeed',  default=7, type=int)
		parser.add_argument('--arenaSz',  default=667, type=int)
		parser.add_argument('--batchSz',  default=16, type=int)
		parser.add_argument('--imSz'   ,  default=128, type=int)
		parser.add_argument('--glimpseSz', default=512, type=int)
		parser.add_argument('--lookAhead',  default=10, type=int)
		parser.add_argument('--history',  default=4, type=int)
		parser.add_argument('--ncpu',   default=6, type=int)
		#The weighting of each step in the horizon for computing the gradients. 
		parser.add_argument('--horWeightType', default='uniform', type=str)
		parser.add_argument('--horWeightParam', default=1, type=float)
		#Whether to have position instead of velocity labels
		parser.add_argument('--posLabel', dest='posLabel', action='store_true', default=False)
		#Whether to have position and velocity as labels or not
		parser.add_argument('--posVel', dest='posVel', action='store_true', default=False)
		#Whether to model forces or not
		parser.add_argument('--isForce', dest='isForce', action='store_true', default=True)
		parser.add_argument('--no-isForce', dest='isForce', action='store_false')
		args   = parser.parse_args(argsStr.split())
		print('Using World Config:')
		pprint.pprint(args)
		return edict(vars(args))	
	
	def setup(self, bottom, top):
		print ('STARTING SETUP')
		#Get the parameters
		self.params_ = DataFetchLayer.parse_args(self.param_str) 
		#Shape the output blobs
		top[0].reshape(self.params_.batchSz, 3 * self.params_.history, 
											 self.params_.imSz, self.params_.imSz)
		top[1].reshape(self.params_.batchSz, self.params_.lookAhead, 2, 1)
		if self.params_.posVel:
			assert (len(top)==4)
			top[2].reshape(self.params_.batchSz, self.params_.lookAhead, 2, 1)
			top[3].reshape(1, self.params_.lookAhead, 2, 1)
		else:
			#Weighting the horizon
			top[2].reshape(1, self.params_.lookAhead, 2, 1)
			
		#Start the pool of workers
		self.pool_   = Pool(processes=self.params_.ncpu)
		self.jobs_   = deque()	
		#Make a Queue for storing the game plays
		self.play_cache_ = Queue.Queue(maxsize=self.params_.batchSz)
		#The datastreams
		self.plays_ = []
		self.plays_len_ = [] #Stores the lenght of the games in #frames
		self.plays_toe_ = [] #Stores the time of end of each play
		self.plays_tfs_ = [] #Stores the time of start of each play
		for j in range(self.params_.batchSz):
			self.plays_.append([])
			self.plays_len_.append(0)
			self.plays_toe_.append(0)
			self.plays_tfs_.append(0)
		#Prepare the data and label vectors
		self.imdata_ = np.zeros((self.params_.batchSz, 3 * self.params_.history, 
										self.params_.imSz, self.params_.imSz), np.float32) 
		self.labels_ = np.zeros((self.params_.batchSz, self.params_.lookAhead, 
										2, 1), np.float32)
		self.labels2_ = np.zeros((self.params_.batchSz, self.params_.lookAhead, 
										2, 1), np.float32)
		if self.params_.horWeightType == 'uniform':
			self.horWeights_ = self.params_.horWeightParam * np.ones((1,
												self.params_.lookAhead, 2, 1), np.float32)
		#Set the mean and scaling
		self.preproc  = edict()
		if self.params_.whiteMean:
			self.preproc['mu'] = 128
		else:
			self.preproc['mu'] = 0
		self.preproc['scale'] = self.params_.scale	

		self.tLast_ = time.time()	
		print ('SETUP DONE')
		#Start loading the data
		self.prefetch()
		

	def prefetch(self):
		rnd     = int(time.time())
		jobArgs = [] 
		for i in range(self.params_.batchSz):
			#ds  = dio.DataSaver(randSeed=rnd+i, **self.params_)	
			jobArgs.append([rnd + i, self.params_])
		try:
			self.jobs_ = self.pool_.map_async(wrapper_fetch_data, jobArgs)
			print ('Data Fetch started')
		except KeyboardInterrupt:
			print 'Keyboard Interrupt received - terminating in launch jobs'
			self.pool_.terminate()	

	def get_cache_data(self):
		#If the plays_ Queue is empty get the data from prefetch
		#and populate the queue. 
		if self.play_cache_.empty():
			try:
				data = self.jobs_.get()
			except KeyboardInterrupt:
				print 'Keyboard Interrupt received - terminating'
				self.pool_.terminate()
			for d in data:
				self.play_cache_.put(d)
			self.prefetch()
		#Return one of the plays from the Queue
		return self.play_cache_.get()

	def preproc_image(self, im):
		#Resize and make it in the format ch, h, w
		im = scm.imresize(im, (self.params_.imSz, self.params_.imSz)).transpose((2,0,1))
		im = im - self.preproc.mu	
		im = im * self.preproc.scale
		return im

	##
	def get_next_sample(self):
		for j in range(self.params_.batchSz):
			#Ensure samples can be taken for all batches
			if self.plays_tfs_[j] + self.params_.lookAhead >= self.plays_len_[j]:
				#Fetch a new example
				self.plays_[j]     = self.get_cache_data()	
				self.plays_len_[j] = len(self.plays_[j][0][0])
				if self.params_.isForce:	
					self.plays_tfs_[j] = 0
				else:
					self.plays_tfs_[j] = self.params_.history
	
			#Get the data
			imBalls, force, vel, pos = self.plays_[j]
			imBall = imBalls[0] #Chosing the first ball by default
			for h in range(self.params_.history):
				stCh = h * 3 #3 channels - RGB
				enCh = stCh + 3
				h = max(0, self.plays_tfs_[j] + h - self.params_.history + 1)
				ylen, xlen, numCh = imBall[h].shape
				self.imdata_[j, stCh:enCh, :, :] = self.preproc_image(imBall[h])
				#For obtaining normalized positions/velocities
				yScale, xScale = float(self.params_.imSz)/ylen, float(self.params_.imSz)/xlen
				posScale = np.array([xScale, yScale]).reshape((2,1))
			stLbl = self.plays_tfs_[j]
			enLbl = stLbl + self.params_.lookAhead
			for l in range(stLbl, enLbl):
				#Choose the first ball
				if self.params_.posVel:
					self.labels_[j,l-stLbl, 0:2]  = pos[0:2,l].reshape((2,1)) * posScale 
					self.labels2_[j,l-stLbl, 0:2] = vel[0:2,l].reshape((2,1))/10000 * posScale
				else:
					if self.params_.posLabel:
						#Position labels required
						self.labels_[j,l-stLbl, 0:2] = pos[0:2,l].reshape((2,1)) * posScale 
					else:
						self.labels_[j,l-stLbl, 0:2] = vel[0:2,l].reshape((2,1))/10000 * posScale
			#Update the counts
			#print (posScale)
			self.plays_tfs_[j] += 1
			
							
	def forward(self, bottom, top):
		#Get the data from the already launched processes
		t1 = time.time()
		#print ('GET NEXT SAMPLE')
		self.get_next_sample()
		t2= time.time()
		tFetch = t2 - t1
		top[0].data[...] = self.imdata_[...]
		top[1].data[...] = self.labels_[...]
		if self.params_.posVel:
			top[2].data[...] = self.labels2_[...]
			top[3].data[...] = self.horWeights_[...]
		else:
			top[2].data[...] = self.horWeights_[...]
		tForward    = time.time() - self.tLast_
		self.tLast_ = time.time()
		glog.info('Forward: %f, Waiting for fetch: %f' % (tForward, tFetch))

	def backward(self, top, propagate_down, bottom):
		""" This layer has no backward """
		pass

	def reshape(self, bottom, top):
		""" This layer has no reshape """
		pass

def vis_im(ax, ims, pos, vel):
	im = ims[0:3,:,:].transpose((1,2,0))
	im = np.uint8(im)
	#print im.shape
	ax.imshow(im)	
	scale  = 10000
	x, y = pos[0].squeeze()
	cIdx = (255.0/20) * np.array(range(20))
	for i in range(pos.shape[0]):
		px, py = pos[i].squeeze()
		vx, vy = vel[i].squeeze()
		vx    = vx * scale
		vy    = vy * scale
		x     = x + vx
		y     = y + vy
		#print (x, px, y, py)
		ax.plot(round(x), round(y), '.', color=cmap.jet(int(cIdx[i])),  markersize=10) 
	plt.draw()
	plt.show()

def test_billiards_layer():
	net = caffe.Net('test/billiards.prototxt', caffe.TEST)
	plt.ion()
	fig = plt.figure()
	ax = []
	for p in range(4):
		ax.append(fig.add_subplot(2,2,p+1))
	for i in range(10000):
		data = net.forward(blobs=['data', 'pos', 'vel'])
		im, pos, vel   = data['data'], data['pos'], data['vel']
		for p in range(4):	
			vis_im(ax[p], im[0][p*3:p*3+3], pos[0], vel[0])
		print ('Waiting ...', i)
		ip = raw_input()
		if ip=='q':
			return
		for p in range(4):	
			ax[p].clear()
