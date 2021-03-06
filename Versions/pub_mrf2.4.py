#!/usr/bin/env python

import numpy as np
from scipy.sparse import csgraph
from scipy.spatial import KDTree

import SimpleITK as sitk

from argparse import ArgumentParser, ArgumentTypeError
from warnings import warn
import os.path
import sys

class AWoL_MRF:
    """The AWoL-MRF algorithm organizes the low-confidence voxels in patches. 
    In each patch, the labels for these voxels are updated in a sequence given
    by Prim's algorithm using the Markov Random Field potentials.
    
    Key features of this version:
    - Works with a single structural label
    - Works with 2 or more separate structural labels
    - Untested with 2 or more adjacent structural labels
    - Assumes the background label is 0 (or smaller than any structural label)
    - Assumes strictly positive integer values for the structural labels
    - Compatible with a Python notebook, see AWoL-MRF_on_notebook.ipynb
    - Uses smart bounding boxes and label counting to reduce peak memory usage
    - Uses number of neighbor high-confidence voxels to rank seeds
    - Skips the patches that wouldn't update any additional voxel
    - Uses dymanic thresholds which depend on the number of candidate labels"""
    
    def __init__(self, labelimg_list, brainimg, bbox=None, beta=-.2, mixing_ratio=10,
                 patch_length=5, same_threshold=True, thresholds=[0.2, 0.2]): #use same defaults as the parser
                
        def positive_int(x): #avoid nonsense negative parameter values   
            x = int(x)
            if x < 0:
                raise AssertionError("%r is not a positive int"%(x,))
            return x
            
        def restricted_float(x): #avoid nonsense values for the threshold
            x = float(x)
            if x < 0.0 or x > 1.0:
                raise AssertionError("%r not in range [0.0, 1.0]"%(x,))
            return x
            
        #catch invalid parameters
        self.beta = float(beta)        
        self.mixing_ratio = positive_int(mixing_ratio)
        self.patch_length = positive_int(patch_length)
        for threshold in thresholds:
            threshold = restricted_float(threshold)

        if bbox is not None:        
            bbox[:3] -= self.patch_length #pad the bounding box with the patch length
            bbox[3:] += self.patch_length
                       
              
        nimg = len(labelimg_list)
        for n, img in enumerate(labelimg_list):
            if bbox is None:
                label_array = sitk.GetArrayFromImage(img) #get the label array from each image
            else: #get each array within the bounding box
                label_array = sitk.GetArrayFromImage(img)[bbox[2]:bbox[5], bbox[1]:bbox[4], bbox[0]:bbox[3]]
                
            if n == 0:
                self.label_values = np.unique(label_array) #obtain the list of labels
                votes = np.zeros((self.label_values.shape[0], label_array.shape[0], 
                                  label_array.shape[1], label_array.shape[2]))
            elif np.asarray(np.unique(label_array) != self.label_values).any(): #make sure that they are the same in each image
                raise AssertionError("Labels in {0} not the same as in {1}.".format(img, labelimg_list[0]))
            
            for i, value in enumerate(self.label_values):
                votes[i][np.where(label_array == value)] += 1 #count the votes for each label
        
        if len(self.label_values) != len(thresholds):
            if not(same_threshold):
                raise AssertionError("Number of labels does not match number of thresholds.")
            else:
                while len(thresholds) < len(self.label_values):
                    thresholds.append(thresholds[-1]) #same threshold for each structural label
        
        self.mode = (np.argmax(votes, axis=0), np.amax(votes, axis=0)) #find the majority votes        
        self.labels = np.zeros(votes[0].shape, dtype=np.int16) - 1 #array of labels
        
        if bbox is None:
            self.intensity = sitk.GetArrayFromImage(brainimg) #array of intensities
        else:
            self.intensity = sitk.GetArrayFromImage(brainimg)[bbox[2]:bbox[5], bbox[1]:bbox[4], bbox[0]:bbox[3]]
        
        #find the high-confidence voxels for each label        
        for i, value in enumerate(self.label_values.tolist()):
            n_labels = np.sum(votes != 0, axis=0) #number of candidate labels for each voxel
            min_votes = (1.0/n_labels + thresholds[i])*nimg
            above_threshold = np.where((self.mode[0] == i) & ((n_labels == 1) | (self.mode[1] >= min_votes)))
            self.labels[above_threshold] = value
            
        self.bbox = bbox #keep the bounding box for the final fusion labels
        self.brainimg = brainimg #keep this to copy the metadata to the output image
            
    def run(self):
        """Find the seeds. For each seed, get the minimum spanning tree
        sequence, and compute the MRF potentials in that order. Then compute
        the final fusion labels."""
             
        self.find_lcv()
        
        if self.no_lcv:
            warn("No low-confidence voxel was found.")
            return self.get_output_image() #return majority vote output
        
        else:
            self.find_seeds()
            
            while self.seeds:
                self.get_patch()
                
                if self.do_walk:
                    self.get_mst_sequence()
                    self.mrf_potentials()
    
            self.final_labels()
            return self.get_output_image()
        
    def find_lcv(self):
        """Find the low-confidence voxels. Also initialize the 6-voxel
        neighborhood and the 26-voxel neighborhood for each low-confidence
        voxel."""
        
        #find the low-confidence voxels
        self.lcv = np.ravel_multi_index(np.where(self.labels == -1), self.labels.shape)
        
        if self.lcv.shape[0] == 0: #in this case we just want to return the majority vote
            self.no_lcv = True
            
        else:
            self.no_lcv = False
            lcv = np.argwhere(self.labels == -1)
            tree_lcv = KDTree(lcv) #create the lcv tree
            
            #create a tree with all the coordinates        
            self.tree = KDTree(np.argwhere(np.isfinite(self.labels)))
            
            #function that finds the neighbors of some voxels of interest
            def neighbors(tree_lcv, tree, dist_max):
                neighbors = {}            
                qbt = tree_lcv.query_ball_tree(tree, dist_max)
                
                #remove each point from its own list of neighbors
                for i, elt in enumerate(qbt):
                    qbt[i].remove(self.lcv[i])
                    neighbors[self.lcv[i]] = sorted(qbt[i]) #rank neighbors in ascending order
                    
                return neighbors
                    
            self.neighbors_small = neighbors(tree_lcv, self.tree, 1) #6-voxel neighborhood        
            self.neighbors_big = neighbors(tree_lcv, self.tree, np.sqrt(3)) #26-voxel neighborhood
            
            self.label_count = np.zeros((self.labels.ravel().shape[0], self.label_values.shape[0]), dtype=np.uint16)
            self.new_labels = np.copy(self.labels) #initialize the array of updated labels
            
    def find_seeds(self):
        """Find the seeds for the AWoL-MRF patches. We assume that each seed 
        needs a minimum number of high-confidence voxels in its 26-voxel
        neighbourhood, which is determined by the mixing ratio parameter."""
        
        self.seeds = []
        confidence_level = []
        lflat = self.labels.ravel()
        
        for lcv in self.lcv:
            n_hcv = sum(lflat[self.neighbors_big[lcv]] != -1)
                
            if n_hcv > self.mixing_ratio: #minimum confidence level for seeds
                confidence_level.append(n_hcv)
                self.seeds.append(lcv)
                    
        del self.neighbors_big
        
        if len(self.seeds) > 0: #sort the seeds based on their confidence level
            self.seeds = [seed for (c, seed) in sorted(zip(confidence_level, self.seeds))][::-1]
            seed_coord = [np.unravel_index(seed, self.labels.shape) for seed in self.seeds]
        
            #find the patch for each seed using KDTree
            tree_seeds = KDTree(seed_coord)        
            self.patches = tree_seeds.query_ball_tree(self.tree, self.patch_length)
            for i, elt in enumerate(self.patches):
                self.patches[i].sort()
                
    def get_patch(self):
        """Finds all the voxels in the patch for the corresponding seed. If 
        at least 1 low-confidence voxel in that patch has not been updated
        yet, AWoL-MRF will compute the MRF potentials in the patch."""
        
        still_lcv_patch = np.asarray(self.patches[0])[self.new_labels.ravel()[self.patches[0]] == -1]
        
        if still_lcv_patch.shape[0] > 0: #if at least 1 lcv wasn't updated yet
            #find all the low-confidence voxels in the patch
            lflat = self.labels.ravel()
            self.lcvp = np.asarray(self.patches[0])[lflat[self.patches[0]] == -1]
        
            #keep the patch stats for each label that is in the patch
            self.patch_stats = {}
            iflat = self.intensity.ravel()
            for value in self.label_values:
                points = iflat[np.asarray(self.patches[0])[lflat[self.patches[0]] == value]]
                if len(points) > 1: #need at least 2 points to consider a label in the patch
                    self.patch_stats[value] = [np.mean(points), np.std(points)]
                
            self.do_walk = True
            del self.patches[0]
            
        else: # if all the lcv have already been updated at least once
            self.do_walk = False
            del self.patches[0], self.seeds[0]
    
    def get_mst_sequence(self):
        """Finds the minimum spanning tree sequence for the low-confidence
        voxels in the patch around the seed. The dimensions of the patch are
        defined by the patch length parameter."""
        
        iflat = self.intensity.ravel()        
        
        #fill the weight matrix for the MST
        n = np.shape(self.lcvp)[0] #the number of lcv in the patch
        weightm = np.zeros((n, n))
        for wx in range(0, n):
            for wy in range(wx, n):
                if self.lcvp[wx] in self.neighbors_small[self.lcvp[wy]]:
                    weightm[wx][wy] = abs(iflat[self.lcvp[wx]] - iflat[self.lcvp[wy]]) #intensity gradient
                else:
                    xcoord = np.asarray(np.unravel_index(self.lcvp[wx], self.labels.shape))
                    ycoord = np.asarray(np.unravel_index(self.lcvp[wy], self.labels.shape))
                    weightm[wx][wy] = 100*np.linalg.norm(xcoord - ycoord) #proportional to the norm
                    
        mst = csgraph.minimum_spanning_tree(weightm).toarray() #get the MST
        
        #find the MST sequence        
        self.seq = [np.argwhere(self.lcvp == self.seeds[0])[0][0]]
        edges = np.argwhere(mst).tolist() #edges of the MST
            
        while edges:
            mincost = np.amax(mst) + 1
            for e in edges:
                if (e[0] in self.seq or e[1] in self.seq) and mst[e[0]][e[1]] < mincost:
                    mincost = mst[e[0]][e[1]]
                    nextedge = e #find the edge with minimum cost
                       
            if nextedge[0] in self.seq:
                self.seq.append(nextedge[1])
            else: #if nextedge[1] in self.seq
                self.seq.append(nextedge[0])
            
            edges.remove(nextedge)
          
        self.seq = [self.lcvp[i] for i in self.seq] #get the ordered list of lcv
        
        del self.seeds[0]
        
    def mrf_potentials(self):
        """Compute the Markov Random Field potential for the patch in the
        order given by the minimum spanning tree sequence. The weight of the
        doubleton potentials is determined by the beta patameter."""        
        
        iflat = self.intensity.ravel()
        nlflat = self.new_labels.ravel()
        
        #compute doubleton and singleton potential
        for lcv in self.seq:
            mrf_energy = np.inf
            n = self.neighbors_small[lcv] 
            for value in self.patch_stats.keys():
                (mean, std) = self.patch_stats[value]
                mrf_single = (np.log(np.sqrt(2*np.pi)*std)) + (np.power(iflat[lcv]-mean,2))/(2*np.power(std,2))
                mrf_double = self.beta*(2*sum(nlflat[n] == value) + sum(nlflat[n] == -1) - len(n))
                #the formula for doubleton potentials is Li - L(not i) = 2Li + L(-1) - (nb of neighbors) 
                if (mrf_single + mrf_double) < mrf_energy:
                    mrf_energy = mrf_single + mrf_double #minimize the MRF energy
                    new_label = value #find the label with minimum energy
             
            #update the label count
            self.label_count[lcv][np.where(self.label_values == new_label)[0][0]] += 1
            nlflat[lcv] = self.label_values[np.argmax(self.label_count[lcv])] #this also updates self.new_labels
        
        del self.seq, self.patch_stats
        
    def final_labels(self):
        """Get the labels of the low-confidence voxels after the walk in each
        patch. If a low-confidence voxel is not found in any patch, its final
        label is the majority vote label. Otherwise, its label is the most
        popular label throughout the patches."""
                
        nlflat = self.new_labels.ravel()
        
        #get the final fusion labels
        still_lcv = np.where(nlflat == -1)
        for i, value in enumerate(self.label_values): #assign the majority vote label
            nlflat[still_lcv[0][self.mode[0].ravel()[still_lcv[0]] == i]] = value
        self.labels = self.new_labels
        
    def get_output_image(self):
        """Return the final AWoL-MRF output image with the final fusion label
        for each voxel."""
        
        #pad the bounding box with background labels
        if self.bbox is not None:        
            self.labels = np.pad(self.labels, ((self.bbox[2], self.brainimg.GetDepth() - self.bbox[5]),
                                  (self.bbox[1], self.brainimg.GetHeight() - self.bbox[4]),
                                  (self.bbox[0], self.brainimg.GetWidth() - self.bbox[3])),
                                  "constant", constant_values=0)
        
        #get the output SimpleITK image with fusion labels        
        output_image = sitk.GetImageFromArray(self.labels)
        output_image.CopyInformation(self.brainimg) #copy the metadata
        
        return output_image

if __name__ == "__main__":
    #AWoL-MRF parameters 
    def positive_int(x): #avoid nonsense negative parameter values   
        x = int(x)
        if x < 0:
            raise ArgumentTypeError("%r is not a positive int"%(x,))
        return x
            
    def restricted_float(x): #avoid nonsense values for the threshold
        x = float(x)
        if x < 0.0 or x > 1.0:
            raise ArgumentTypeError("%r not in range [0.0, 1.0]"%(x,))
        return x
    
    parser = ArgumentParser(description="""The AWoL-MRF algorithm organizes the low-confidence voxels in patches. 
                            In each patch, the labels for these voxels are updated in a sequence given
                            by Prim's algorithm using the Markov Random Field potentials.
    
                            Key features of this version:
                            - Works with a single structural label
                            - Works with 2 or more separate structural labels
                            - Untested with 2 or more adjacent structural labels
                            - Assumes the background label is 0 (or smaller than any structural label)
                            - Assumes strictly positive integer values for the structural labels
                            - Compatible with a Python notebook, see AWoL-MRF_on_notebook.ipynb
                            - Uses smart bounding boxes and label counting to reduce peak memory usage
                            - Uses number of neighbor high-confidence voxels to rank seeds
                            - Skips the patches that wouldn't update any additional voxel
                            - Uses dymanic thresholds which depend on the number of candidate labels""")  
                            
    pg = parser.add_argument_group("AWoL-MRF parameters")
    pg.add_argument("-b", "--beta", type=float, default=-.2,
                    help="[default = %(default)s]")
    pg.add_argument("-p", "--patch_length", type=positive_int, default=5,
                    help="[default = %(default)s]")
    pg.add_argument("-r", "--mixing_ratio", type=positive_int, default=10,
                    help="[default = %(default)s]")
    #the thresholds should be in the same order as the labels
    pg.add_argument("-t", "--thresholds", nargs="+", type=restricted_float, 
                    default=[0.2, 0.2], metavar=("T_BACKGROUND", "T_STRUCTURE"),
                    help="""A voxel V has high-confidence for any label L if the number of votes
                    it receives for that label is greater than (1/n(V) + T)*nimg, where n(V) is
                    the number of candidate labels at voxel V. [default = %(default)s]""")
    
    #user can decide if he wants different labels for each structural label
    tg = pg.add_mutually_exclusive_group()
    tg.add_argument("--same-threshold", dest="same_threshold", action="store_true",
                   help="same threshold for each structure [default = %(default)s]")
    tg.add_argument("--different-thresholds", dest="same_threshold", action="store_false",
                   help="opposite of '--same-threshold'")
    tg.set_defaults(same_threshold=True)
                    
    #file manipulation arguments
    parser.add_argument("input_labels", nargs="+", type=str)
    parser.add_argument("--brain_image", type=str, required=True,
                        help="brain intensity image, required argument") #need this for the singleton potentials
    parser.add_argument("output_labels", type=str)
    cg = parser.add_mutually_exclusive_group()
    cg.add_argument("--clobber", dest="clobber", action="store_true",
                   help="clobber output file [default = %(default)s]")
    cg.add_argument("--no-clobber", dest="clobber", action="store_false",
                   help="opposite of '--clobber'")
    cg.set_defaults(clobber=False)

    opt = parser.parse_args()
    

    if not(opt.clobber) and os.path.exists(opt.output_labels):
        sys.exit("Output file already exists; use --clobber to overwrite.")
    
    #load volumes from input files    
    labelimg_list = [] #list of candidate segmentation images
    
    #use this to verify if the voxel-wise computations make sense    
    def check_metadata(img, metadata, filename):
        if img.GetSize() != metadata["size"]:
            sys.exit("Size of {0} not the same as {1}".format(filename, opt.input_labels[0]))
        elif img.GetOrigin() != metadata["origin"]:
            sys.exit("Origin of {0} not the same as {1}".format(filename, opt.input_labels[0]))
        elif img.GetSpacing() != metadata["spacing"]:
            sys.exit("Spacing of {0} not the same as {1}".format(filename, opt.input_labels[0]))
        elif img.GetDirection() != metadata["direction"]:
            sys.exit("Direction of {0} not the same as {1}".format(filename, opt.input_labels[0]))
    
    for filename in opt.input_labels:
        labelimg = sitk.ReadImage(filename) #get all the candidate segmentations
        
        structure = labelimg > 0 #find the structural voxels
        label_shape_analysis = sitk.LabelShapeStatisticsImageFilter()
        label_shape_analysis.SetBackgroundValue(0)
        label_shape_analysis.Execute(structure)
        b = label_shape_analysis.GetBoundingBox(1) #get the bounding box        
        
        if len(labelimg_list) == 0:        
            metadata = {} #get the metadata of the first image
            metadata["size"] = labelimg.GetSize()
            metadata["origin"] = labelimg.GetOrigin()
            metadata["spacing"] = labelimg.GetSpacing()
            metadata["direction"] = labelimg.GetDirection()
            
            #get the first bounding box
            bbox = [b[0], b[1], b[2], b[0]+b[3], b[1]+b[4], b[2]+b[5]]
                
        else: #check that the metadata is the same for each other image
            check_metadata(labelimg, metadata, filename)
            
            new_bbox = (b[0], b[1], b[2], b[0]+b[3], b[1]+b[4], b[2]+b[5])   
            for i in range(0,3): #for each minimum bounding box index
                if new_bbox[i] < bbox[i]:
                    bbox[i] = new_bbox[i] #keep the new minimum
            for i in range(3,6): #for each maximum bounding box index
                if new_bbox[i] > bbox[i]:
                    bbox[i] = new_bbox[i] #keep the new maximum
        
        labelimg_list.append(labelimg)
  
    brainimg = sitk.ReadImage(opt.brain_image) #get the subject brain intensity image
    check_metadata(brainimg, metadata, opt.brain_image)
        
    #go through the AWoL-MRF steps
    awolmrf = AWoL_MRF(labelimg_list, brainimg, bbox=np.asarray(bbox), beta=opt.beta, mixing_ratio=opt.mixing_ratio, 
                       patch_length=opt.patch_length, same_threshold=opt.same_threshold, thresholds=opt.thresholds)
                       
    del labelimg_list
      
    output_image = awolmrf.run()
    
    sitk.WriteImage(output_image, opt.output_labels, True) #save the result to the output file