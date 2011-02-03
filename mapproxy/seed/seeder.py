# This file is part of the MapProxy project.
# Copyright (C) 2010, 2011 Omniscale <http://omniscale.de>
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import with_statement, division
import sys

from mapproxy.config import base_config
from mapproxy.grid import MetaGrid
from mapproxy.source import SourceError
from mapproxy.util import local_base_config
from mapproxy.seed.util import format_task

from mapproxy.seed.util import (timestamp, exp_backoff, ETA, limit_sub_bbox,
    status_symbol, format_bbox)

NONE = 0
CONTAINS = -1
INTERSECTS = 1

# do not use multiprocessing on windows, it blows
# no lambdas, no anonymous functions/classes, no base_config(), etc.
if sys.platform == 'win32':
    import Queue
    import threading
    proc_class = threading.Thread
    queue_class = Queue.Queue
else:
    import multiprocessing
    proc_class = multiprocessing.Process
    queue_class = multiprocessing.Queue


class SeedPool(object):
    """
    Manages multiple SeedWorker.
    """
    def __init__(self, cache, size=2, dry_run=False):
        self.tiles_queue = queue_class(32)
        self.cache = cache
        self.dry_run = dry_run
        self.procs = []
        conf = base_config()
        for _ in xrange(size):
            worker = SeedWorker(cache, self.tiles_queue, conf, dry_run=dry_run)
            worker.start()
            self.procs.append(worker)
    
    def seed(self, tiles, progress):
        self.tiles_queue.put((tiles, progress))
    
    def stop(self):
        for _ in xrange(len(self.procs)):
            self.tiles_queue.put((None, None))
        
        for proc in self.procs:
            proc.join()


class SeedWorker(proc_class):
    def __init__(self, tile_mgr, tiles_queue, conf, dry_run=False):
        proc_class.__init__(self)
        proc_class.daemon = True
        self.tile_mgr = tile_mgr
        self.tiles_queue = tiles_queue
        self.conf = conf
        self.dry_run = dry_run
    def run(self):
        with local_base_config(self.conf):
            while True:
                tiles, progress = self.tiles_queue.get()
                if tiles is None:
                    return
                print '[%s] %6.2f%% %s \tETA: %s\r' % (
                    timestamp(), progress[1]*100, progress[0],
                    progress[2]
                ),
                sys.stdout.flush()
                if not self.dry_run:
                    exp_backoff(self.tile_mgr.load_tile_coords, args=(tiles,),
                                exceptions=(SourceError, IOError))

class Seeder(object):
    def __init__(self, task, seed_pool, skip_geoms_for_last_levels=0):
        self.tile_mgr = task.tile_manager
        self.task = task
        self.seed_pool = seed_pool
        self.skip_geoms_for_last_levels = skip_geoms_for_last_levels
        
        num_seed_levels = len(task.levels)
        self.report_till_level = task.levels[int(num_seed_levels * 0.8)]
        meta_size = self.tile_mgr.meta_grid.meta_size if self.tile_mgr.meta_grid else (1, 1)
        self.tiles_per_metatile = meta_size[0] * meta_size[1]
        self.grid = MetaGrid(self.tile_mgr.grid, meta_size=meta_size, meta_buffer=0)
        self.progress = 0.0
        self.eta = ETA()
        self.count = 0
        

    def seed(self):
        bbox = self.task.coverage.extent.bbox_for(self.tile_mgr.grid.srs)
        self._seed(bbox, self.task.levels)
        self.report_progress(self.task.levels[0], self.task.coverage.bbox)

    def _seed(self, cur_bbox, levels, progess_str='', progress=1.0, all_subtiles=False):
        """
        :param cur_bbox: the bbox to seed in this call
        :param levels: list of levels to seed
        :param all_subtiles: seed all subtiles and do not check for
                             intersections with bbox/geom
        """
        current_level, levels = levels[0], levels[1:]
        bbox_, tiles, subtiles = self.grid.get_affected_level_tiles(cur_bbox, current_level)
        total_sub_seeds = tiles[0] * tiles[1]
        
        if len(levels) < self.skip_geoms_for_last_levels:
            # do not filter in last levels
            all_subtiles = True
        sub_seeds = self._filter_subtiles(subtiles, all_subtiles)
        
        if current_level <= self.report_till_level:
            self.report_progress(current_level, cur_bbox)
        
        progress = progress / total_sub_seeds
        for i, (subtile, sub_bbox, intersection) in enumerate(sub_seeds):
            if subtile is None: # no intersection
                self.progress += progress
                continue
            if levels: # recurse to next level
                sub_bbox = limit_sub_bbox(cur_bbox, sub_bbox)
                cur_progess_str = progess_str + status_symbol(i, total_sub_seeds)
                if intersection == CONTAINS:
                    all_subtiles = True
                else:
                    all_subtiles = False
                self._seed(sub_bbox, levels, cur_progess_str,
                           all_subtiles=all_subtiles, progress=progress)
            
            if not self.tile_mgr.is_cached(subtile):
                self.count += 1
                self.seed_pool.seed([subtile],
                    (progess_str, self.progress, self.eta))

            if not levels:
                self.progress += progress
        
        self.eta.update(self.progress)
    
    def report_progress(self, level, bbox):
        print '[%s] %2s %6.2f%% %s (%d tiles) ETA: %s' % (
            timestamp(), level, self.progress*100,
            format_bbox(bbox), self.count * self.tiles_per_metatile, self.eta)
        sys.stdout.flush()
    
    def _filter_subtiles(self, subtiles, all_subtiles):
        """
        Return an iterator with all sub tiles.
        Yields (None, None, None) for non-intersecting tiles,
        otherwise (subtile, subtile_bbox, intersection).
        """
        for subtile in subtiles:
            if subtile is None:
                yield None, None, None
            else:
                sub_bbox = self.grid.meta_tile(subtile).bbox
                if all_subtiles:
                    intersection = CONTAINS
                else:
                    intersection = self.task.intersects(sub_bbox)
                if intersection:
                    yield subtile, sub_bbox, intersection
                else: 
                    yield None, None, None


class SeedTask(object):
    def __init__(self, md, tile_manager, levels, refresh_timestamp, coverage):
        self.md = md
        self.tile_manager = tile_manager
        self.grid = tile_manager.grid
        self.levels = levels
        self.refresh_timestamp = refresh_timestamp
        self.coverage = coverage
    
    def intersects(self, bbox):
        if self.coverage.contains(bbox, self.grid.srs): return CONTAINS
        if self.coverage.intersects(bbox, self.grid.srs): return INTERSECTS
        return NONE


def seed_tasks(tasks, concurrency=2, dry_run=False, skip_geoms_for_last_levels=0,
               verbose=True):
    for task in tasks:
        print format_task(task)
        if task.refresh_timestamp:
            task.tile_manager._expire_timestamp = task.refresh_timestamp
        # self.seeded_caches.append(tile_mgr)
        task.tile_manager.minimize_meta_requests = False
        seed_pool = SeedPool(task.tile_manager, dry_run=dry_run, size=concurrency)
        seeder = Seeder(task, seed_pool, skip_geoms_for_last_levels=skip_geoms_for_last_levels)
        seeder.seed()
        seed_pool.stop()

# class CacheSeeder(object):
#     """
#     Seed multiple caches with the same option set.
#     """
#     def __init__(self, caches, remove_before, dry_run=False, concurrency=2,
#                  skip_geoms_for_last_levels=0):
#         self.remove_before = remove_before
#         self.dry_run = dry_run
#         self.caches = caches
#         self.concurrency = concurrency
#         self.seeded_caches = []
#         self.skip_geoms_for_last_levels = skip_geoms_for_last_levels
#     
#     def seed_view(self, bbox, level, bbox_srs, cache_srs, geom=None):
#         for srs, tile_mgr in self.caches.iteritems():
#             if not cache_srs or srs in cache_srs:
#                 print "[%s] ... srs '%s'" % (timestamp(), srs.srs_code)
#                 self.seeded_caches.append(tile_mgr)
#                 if self.remove_before:
#                     tile_mgr._expire_timestamp = self.remove_before
#                 tile_mgr.minimize_meta_requests = False
#                 seed_pool = SeedPool(tile_mgr, dry_run=self.dry_run, size=self.concurrency)
#                 seed_task = SeedTask(bbox, level, bbox_srs, srs, geom)
#                 seeder = Seeder(tile_mgr, seed_task, seed_pool, self.skip_geoms_for_last_levels)
#                 seeder.seed()
#                 seed_pool.stop()
#     
#     def cleanup(self):
#         for tile_mgr in self.seeded_caches:
#             for i in range(tile_mgr.grid.levels):
#                 level_dir = tile_mgr.cache.level_location(i)
#                 if self.dry_run:
#                     def file_handler(filename):
#                         print 'removing ' + filename
#                 else:
#                     file_handler = None
#                 print 'removing oldfiles in ' + level_dir
#                 cleanup_directory(level_dir, self.remove_before,
#                     file_handler=file_handler)
