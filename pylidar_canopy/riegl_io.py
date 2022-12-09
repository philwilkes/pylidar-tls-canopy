#!/usr/bin/env python3
"""
pylidar_canopy

Drivers for handling RIEGL rdbx and rxp files

John Armston
University of Maryland
October 2022
"""

import riegl.rdb

import riegl_rxp

import sys
import json
import numpy as np

from . import DEFAULT_RDB_ATTRIBUTES
from . import PRR_MAX_TARGETS 


class RXPFile:
    def __init__(self, filename, transform_file=None, pose_file=None):
        self.filename = filename
        if transform_file is not None:
            self.transform = read_transform_file(transform_file)
        elif pose_file is not None:
            with open(pose_file,'r') as f:
                pose = json.load(f)
            self.transform = calc_transform_matrix(pose['pitch'], pose['roll'], pose['yaw'])
        else:
            self.transform = None

    def __enter__(self):
        self.read_file()
        return self

    def __exit__(self, type, value, traceback):
        pass

    def read_file(self):
        """
        Read file and get global stats
        """
        self.meta, points, pulses = riegl_rxp.readFile(self.filename)

        self.minc = 0
        self.maxc = np.max(pulses['scanline'])
        self.minr = 0
        self.maxr = np.max(pulses['scanline_idx'])
        self.max_target_count = np.max(pulses['target_count'])
        self.max_range = np.max(points['range'])

        if self.transform is None:
            if 'PITCH' in self.meta:
                self.transform = calc_transform_matrix(self.meta['PITCH'], self.meta['ROLL'], self.meta['YAW'])
        
        self.pulses = {}
        if self.transform is not None:
            xyz = np.vstack((pulses['beam_direction_x'], pulses['beam_direction_y'], pulses['beam_direction_z'])).T
            xyz_t = apply_transformation(xyz, xyz.shape[0], self.transform)
            _, self.pulses['zenith'], self.pulses['azimuth'] = xyz2rza(xyz_t[:,0], xyz_t[:,1], xyz_t[:,2])
        self.pulses['valid'] = pulses['scanline'] >= 0

        self.points = {}
        if self.transform is not None:
            xyz = np.vstack((points['x'], points['y'], points['z'])).T
            xyz_t = apply_transformation(xyz, xyz.shape[0], self.transform)
            self.points['x'] = xyz_t[:,0]
            self.points['y'] = xyz_t[:,1]
            self.points['z'] = xyz_t[:,2]
        self.points['valid'] = np.repeat(pulses['scanline'], pulses['target_count']) >= 0

        for name in pulses.dtype.names:
            if name not in self.pulses:
                self.pulses[name] = pulses[name]
            
        for name in points.dtype.names:
            if name not in self.points:
                self.points[name] = points[name]

    def get_data(self, name, return_as_point_attribute=False):
        """
        Get a pulse or point attribute
        """
        if name in self.pulses:
            data = self.pulses[name]
            valid = self.pulses['valid']
            if return_as_point_attribute:
                data = np.repeat(data, self.pulses['target_count'])
                valid = self.points['valid'] 
        elif name in self.points:
            data = self.points[name]
            valid = self.points['valid']
        else:
            print(f'{name:} is not a pulse or point attribute')
            sys.exit()
        
        return data[valid]


class RDBFile:
    def __init__(self, filename, attributes=DEFAULT_RDB_ATTRIBUTES, chunk_size=100000, 
        transform_file=None, query_str=None, first_only=False):
        self.filename = filename
        self.point_attributes = attributes
        self.chunk_size = chunk_size
        if first_only:
            self.query_str = '(riegl.target_index == 1)'
        else:
            self.query_str = query_str
        self.transform_file = transform_file
        self.query = None

    def __enter__(self):
        self.open_file()
        return self

    def __exit__(self, type, value, traceback):
        self.rdb.close()

    def open_file(self):
        """
        Open file, create attribute buffers, and get global stats
        """
        self.rdb = riegl.rdb.rdb_open(self.filename)
        self.points = {}
        for p in self.point_attributes:
            name = self.point_attributes[p]
            self.points[name] = riegl.rdb.AttributeBuffer(self.rdb.point_attributes[p], self.chunk_size)

        with self.rdb.stat as stat:
            self.point_count_current = 0
            self.point_count_total = stat.point_count_total
            self.minx,self.miny,self.minz = stat.minimum_riegl_xyz
            self.maxx,self.maxy,self.maxz = stat.maximum_riegl_xyz
            if 'riegl.scan_line_index' in self.point_attributes: 
                self.minc = stat.minimum_riegl_scan_line_index
                self.maxc = stat.maximum_riegl_scan_line_index
                self.minr = stat.minimum_riegl_shot_index_line
                self.maxr = stat.maximum_riegl_shot_index_line
            self.max_target_count = stat.maximum_riegl_target_count
            self.max_range = np.sqrt(self.maxx**2 + self.maxy**2 + self.maxz**2)

        if self.transform_file is not None:
            self.transform = read_transform_file(self.transform_file)
        else:
            pose = self.get_meta('riegl.pose_estimation')
            self.transform = calc_transform_matrix(pose['orientation']['pitch'], 
                pose['orientation']['roll'], pose['orientation']['yaw']) 

    def read_next_chunk(self):
        """
        Iterate the point cloud chunk-wise
        """
        if self.query is None:
            self.query = self.rdb.select(self.query_str)
            self.point_count = 1
            for k in self.points:
                self.query.bind(self.points[k])

        if self.point_count > 0:
            self.point_count = self.query.next(self.chunk_size)
            if 'riegl_xyz' in self.points:
                xyz_t = apply_transformation(self.points['riegl_xyz'], self.chunk_size, self.transform)
                self.points['x'] = xyz_t[:,0]
                self.points['y'] = xyz_t[:,1]
                self.points['z'] = xyz_t[:,2]
                self.points['range'],self.points['zenith'],self.points['azimuth'] = xyz2rza(xyz_t[:,0], 
                    xyz_t[:,1], xyz_t[:,2])
            self.point_count_current += self.point_count
        else:
            self.point_count_total = self.point_count_current

    def get_chunk(self, name):
        """
        Return the chunk of data
        """
        return self.points[name][0:self.point_count]

    def get_meta(self, key):
        """
        Get an individual metadata item
        'riegl.pose_estimation', 'riegl.geo_tag', 'riegl.notch_filter', 'riegl.window_echo_correction',
        'riegl.detection_probability', 'riegl.angular_notch_filter', 'riegl.pulse_position_modulation',
        'riegl.noise_estimates', 'riegl.device', 'riegl.atmosphere', 'riegl.near_range_correction', 
        'riegl.time_base', 'riegl.scan_pattern', 'riegl.device_geometry', 'riegl.range_statistics', 
        'riegl.beam_geometry', 'riegl.reflectance_calculation', 'riegl.window_analysis',
        'riegl.mta_settings', 'riegl.point_attribute_groups'        
        """
        return json.loads(self.rdb.meta_data[key])

    def get_attr(self, key, name=None):
        """
        Get an individual attribute item
        """
        if name is not None:
            val = self.rdb.point_attributes[key][name]
        else:
            val =  self.rdb.point_attributes[key]
        return val


def get_rdb_point_attributes(filename):
    """
    Get point attributes
    """
    d = {}
    with riegl.rdb.rdb_open(filename) as rdb:
        for attribute in rdb.point_attributes.values():
            group, index = rdb.point_attributes.group(attribute.name)
            if group in d:
                d[group].append(attribute.name)
            else:
                d[group] = []
    return d


def calc_transform_matrix(pitch, roll, yaw):
    """
    Get transform matrix
    Set compass reading to zero if nan
    """
    pitch = np.radians(pitch)
    pitch_mat = np.identity(4)
    pitch_mat[0,0] = np.cos(pitch)
    pitch_mat[0,2] = np.sin(pitch)
    pitch_mat[2,0] = -np.sin(pitch)
    pitch_mat[2,2] = np.cos(pitch)

    roll = np.radians(roll)
    roll_mat = np.identity(4)
    roll_mat[1,1] = np.cos(roll)
    roll_mat[1,2] = -np.sin(roll)
    roll_mat[2,1] = np.sin(roll)
    roll_mat[2,2] = np.cos(roll)

    yaw = np.radians(yaw)
    if np.isnan(yaw):
        yaw = 0.0
    yaw_mat = np.identity(4)
    yaw_mat[0,0] = np.cos(yaw)
    yaw_mat[0,1] = -np.sin(yaw)
    yaw_mat[1,0] = np.sin(yaw)
    yaw_mat[1,1] = np.cos(yaw)

    tmp_mat = yaw_mat.dot(pitch_mat)
    transform = tmp_mat.dot(roll_mat)

    return transform


def apply_transformation(xyz, size, transform_matrix, translate=False):
    """
    Apply transformation
    d: apply transformation (1) or rotation only (0)
    """
    if translate:
        t = np.ones((size,1))
    else:
        t = np.zeros((size,1))

    xyz = np.concatenate((xyz, t), 1)
    xyz_t = np.dot(xyz, transform_matrix)

    return xyz_t[:,0:3]


def xyz2rza(x, y, z):
    """
    Calculate spherical coordinates from the xyz data
    """
    r = np.sqrt(x**2 + y**2 + z**2)
    theta = np.arccos(z / r)
    phi = np.arctan2(x, y)
    np.add(phi, 2*np.pi, out=phi, where=x < 0)

    return r, theta, phi


def read_transform_file(fn):
    """
    Read the transform matrix (rotation + translation) 
    """
    with open(fn, 'rb') as f:
        transform = np.loadtxt(f, delimiter=' ', dtype=np.float32)
    return transform.T
