#!/usr/bin/python3
import csv
import glob
import os
import re
import datetime
import itertools
import json
import numpy as np
import subprocess
import sys
import socket
import copy
from bs4 import BeautifulSoup
from distutils.dir_util import copy_tree

from sofa_print import *

def ds_preprocess(cfg):
    from sofa_preprocess import sofa_preprocess
    from sofa_analyze import sofa_analyze

    save_logdir = cfg.logdir
    # ds_logpath = cfg.logdir + "ds_finish/"
    # os.chdir(ds_logpath)

    nodes_record_dir = []
    for dir in filter(lambda x: os.path.isdir(x), os.listdir('.')):
        if dir.find('_sofalog') == -1:
            continue
        nodes_record_dir.append(dir)

    sofa_timebase_min = sys.maxsize
    for i in range(len(nodes_record_dir)):
        time_fd = open('%s/sofa_time.txt' % nodes_record_dir[i])
        unix_time = time_fd.readline()
        unix_time.rstrip()
        unix_time = float(unix_time)

        # get minimum timebase among sofalog directories
        sofa_timebase_min = min(sofa_timebase_min, unix_time)

    for i in range(len(nodes_record_dir)):
        time_fd = open('%s/sofa_time.txt' % nodes_record_dir[i])
        unix_time = time_fd.readline()
        unix_time.rstrip()
        unix_time = float(unix_time)
        cfg.cpu_time_offset = 0
        if (unix_time > sofa_timebase_min):
            basss = float(sofa_timebase_min) - float(unix_time)
            if basss < -28700:
                basss += 28800
            cfg.cpu_time_offset = basss
            # cfg.cpu_time_offset = float(sofa_timebase_min) - float(unix_time)
        print("%s, %f" % (nodes_record_dir[i], cfg.cpu_time_offset))

        cfg.logdir = './' + str(nodes_record_dir[i]) + '/'
        sofa_preprocess(cfg)
        sofa_analyze(cfg)

    # pid2y_pos_dic = ds_connect_preprocess(cfg)
    # dds_calc_topic_latency(cfg)
    #ds_dds_create_span(cfg)

def ds_viz(cfg):
    nodes_record_dir = []
    for dir in filter(lambda x: os.path.isdir(x), os.listdir('.')):
        if dir.find('_sofalog') == -1:
            continue
        nodes_record_dir.append(dir)

    local = os.path.basename(os.path.dirname(cfg.logdir))
    idx = nodes_record_dir.index(local)
    nodes_record_dir.pop(idx)

    master = BeautifulSoup(open(os.path.join(cfg.logdir, 'index.html')), 'html.parser')
    with open(os.path.join(local, 'timeline.js')) as f:
        sofa_fig_highchart = f.read()
    sidenav = master.find('div', {'class': 'sidenav'})
    subtitle = master.new_tag('figcaption', **{'class': 'sidenav-element-title'})
    subtitle.string = socket.gethostname()
    sidenav.insert(0, subtitle)

    for i, dir in enumerate(nodes_record_dir, 2):
        hostname = re.sub('_sofalog$', '', dir)
        subtitle = master.new_tag('figcaption', **{'class': 'sidenav-element-title'})
        subtitle.string = hostname
        sidenav.append(subtitle)
        
        # sofa figure
        sofa_fig = BeautifulSoup('''
        <a name="e">
        <div id="container" style="min-width: 310px; height: 400px; max-width: 90%; margin-left: 280px; padding-top: 50px">
        </div></a>        
        ''')

        sofa_fig.a['name'] = 'e' + str(i)
        sofa_fig.div['id'] = 'container' + str(i)
        new_sofa_fig_highchart = sofa_fig_highchart.replace('container',
                                                            'container' + str(i))
        sofa_fig_sidenav = BeautifulSoup('''
        <a href="#e"><i class="fa fa-image"></i> Functions and Events</a>
        ''')
        sofa_fig_sidenav.a['href'] = '#e' + str(i)
        sidenav.append(sofa_fig_sidenav.a)

        report   = master.new_tag('script', src=os.path.join(dir, 'report.js'))
        timeline = master.new_tag('script', src=os.path.join(dir, 'timeline.js'))

        master.body.append(sofa_fig.a)
        master.body.append(report)
        master.body.append(timeline)
        
        copied_sofalog_dir = os.path.join(local, dir)
        copy_tree(dir, copied_sofalog_dir)
        with open(os.path.join(copied_sofalog_dir, 'timeline.js'), 'w') as f:
            f.write(new_sofa_fig_highchart)
        
        # Plotly
        sofa_plotly = master.find('a', attrs={'name':'n'})
        sofa_plotly = copy.copy(sofa_plotly)
        sofa_plotly['name'] = 'n' + str(i)
        sofa_plotly.div['id'] = 'main_net' + str(i)
        sofa_plotly.script.string = sofa_plotly.script.string.replace('netbandwidth.csv',
            os.path.join(dir, 'netbandwidth.csv'))
        sofa_plotly.script.string = sofa_plotly.script.string.replace('main_net', 'main_net' + str(i))
        sofa_plotly_sidenav = BeautifulSoup('''
        <a href="#n"><i class="fa fa-line-chart"></i> Network Utilization</a>
        ''')
        sofa_plotly_sidenav.a['href'] = '#n' + str(i)
        sidenav.append(sofa_plotly_sidenav.a)

        master.body.append(sofa_plotly)

        print(master.prettify())

    with open(os.path.join(cfg.logdir, 'index.html'), 'w') as f:
        f.write(master.prettify())