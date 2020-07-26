#!/usr/bin/python3

import sys
import pandas as pd
import numpy as np
import os
import concurrent.futures
import functools, itertools
import sofa_time
import statistics
import multiprocessing as mp
import socket
import ipaddress

# sys.path.insert(0, '/home/st9540808/Desktop/sofa/bin')
import sofa_models, sofa_preprocess
import sofa_config
import sofa_print

colors_send = ['#14f2e0', '#41c8e5', '#6e9eeb']
colors_recv = ['#9a75f0', '#c74bf6', '#f320fa', '#fe2bcc']
color_send = itertools.cycle(colors_send)
color_recv = itertools.cycle(colors_recv)

sofa_ros2_fieldnames = [
    "timestamp",  # 0
    "event",      # 1
    "duration",   # 2
    "deviceId",   # 3
    "copyKind",   # 4
    "payload",    # 5
    "bandwidth",  # 6
    "pkt_src",    # 7
    "pkt_dst",    # 8
    "pid",        # 9
    "tid",        # 10
    "name",       # 11
    "category",   # 12
    "unit",       # 13
    "msg_id"]     # 14

# @profile
def extract_individual_rosmsg(df_send_, df_recv_, *df_others_):
    """ Return a dictionary with topic name as key and
        a list of ros message as value.
        Structure of return value: {topic_name: {(guid, seqnum): log}}
        where (guid, seqnum) is a msg_id
    """
    # Convert timestamp to unix time
    # unix_time_off = statistics.median(sofa_time.get_unix_mono_diff() for i in range(100))
    # for df in (df_send, df_recv, *df_others):
    #     df['ts'] = df['ts'] + unix_time_off
    df_send_[1]['ts'] = df_send_[1]['ts'] + df_send_[0].cpu_time_offset + df_send_[0].unix_time_off
    df_recv_[1]['ts'] = df_recv_[1]['ts'] + df_recv_[0].cpu_time_offset + df_recv_[0].unix_time_off
    df_others = []
    for cfg_to_pass, df_other in df_others_:
        df_other['ts'] = df_other['ts'] + cfg_to_pass.cpu_time_offset + cfg_to_pass.unix_time_off
        df_others.append(df_other)

    df_send = df_send_[1]
    df_recv = df_recv_[1]

    # sort by timestamp
    df_send.sort_values(by=['ts'], ignore_index=True)
    df_recv.sort_values(by=['ts'], ignore_index=True)

    # publish side
    gb_send = df_send.groupby('guid')
    all_publishers_log = {guid:log for guid, log in gb_send}

    # subscription side
    gb_recv = df_recv.groupby('guid')
    all_subscriptions_log = {guid:log for guid, log in gb_recv}

    # other logs (assume there's no happen-before relations that needed to be resolved)
    # every dataframe is a dictionary in `other_log_list`
    gb_others = [df_other.groupby('guid') for df_other in df_others]
    other_log_list = [{guid:log for guid, log in gb_other} for gb_other in gb_others]

    # find guids that are in both subsciption and publisher log
    interested_guids = all_subscriptions_log.keys() \
                     & all_publishers_log.keys()

    res = {}
    for guid in interested_guids:
        # get a publisher from log
        df = all_publishers_log[guid]
        df_send_partial = all_publishers_log[guid].copy()
        add_data_calls = df[~pd.isna(df['seqnum'])] # get all non-NaN seqnums in log
        try:
            pubaddr, = pd.unique(df['publisher']).dropna()
            print(pubaddr)
        except ValueError as e:
            print('Find a guid that is not associated with a publisher memory address. Error: ' + str(e))
            continue
        # print(add_data_calls)

        all_RTPSMsg_idx = ((df_send['func'] == '~RTPSMessageGroup') & (df_send['publisher'] == pubaddr))
        all_RTPSMsgret_idx = ((df_send['func'] == '~RTPSMessageGroup exit') & (df_send['publisher'] == pubaddr))
        all_sendSync_idx = ((df_send['func'] == 'sendSync') & (df_send['publisher'] == pubaddr))
        all_nn_xpack_idx = (df['func'] == 'nn_xpack_send1')
        modified_rows = []
        for idx, add_data_call in add_data_calls.iterrows():
            ts = add_data_call['ts']

            rcl_idx = df.loc[(df['ts'] < ts) & (df['layer'] == 'rcl')]['ts'].idxmax()
            df_send_partial.loc[rcl_idx, 'seqnum'] = add_data_call.loc['seqnum']

            # For grouping RTPSMessageGroup function
            try:
                ts_gt = (df_send['ts'] > ts) # ts greater than that of add_data_call

                RTPSMsg_idx = df_send.loc[ts_gt & all_RTPSMsg_idx]['ts'].idxmin()
                modified_row = df_send.loc[RTPSMsg_idx]
                modified_row.at['seqnum'] = add_data_call.loc['seqnum']
                modified_row.at['guid'] = guid
                modified_rows.append(modified_row)

                RTPSMsgret_idx = df_send.loc[ts_gt & all_RTPSMsgret_idx]['ts'].idxmin()
                modified_row = df_send.loc[RTPSMsgret_idx]
                modified_row.at['seqnum'] = add_data_call.loc['seqnum']
                modified_row.at['guid'] = guid
                modified_rows.append(modified_row)

                sendSync_idx = df_send.loc[ts_gt & (df_send['ts'] < df_send.loc[RTPSMsgret_idx, 'ts']) & all_sendSync_idx]
                sendSync = sendSync_idx.copy()
                sendSync['seqnum'] = add_data_call.loc['seqnum']
                modified_rows.extend(row for _, row in sendSync.iterrows())
            except ValueError as e:
                pass

            if 'rmw_cyclonedds_cpp' in df['implementation'].values:
                try:
                    df_cls = other_log_list[0][guid]
                    seqnum = add_data_call.loc['seqnum']
                    max_ts = df_cls[(df_cls['layer'] == 'cls_egress') & (df_cls['seqnum'] == seqnum)]['ts'].max()
                    index = df.loc[(ts < df['ts']) & (df['ts'] < max_ts) & all_nn_xpack_idx].index
                    df_send_partial.loc[index, 'seqnum'] = seqnum
                except ValueError as e:
                    pass
        df_send_partial = pd.concat([df_send_partial, pd.DataFrame(modified_rows)])

        # get a subscrption from log
        df = all_subscriptions_log[guid]
        df_recv_partial = all_subscriptions_log[guid].copy()
        add_recvchange_calls = df[~pd.isna(df['seqnum'])] # get all not nan seqnums in log
        if 'cyclonedds' in df['layer'].unique():
            add_recvchange_calls = df[df['func'] == 'ddsi_udp_conn_read exit']

        all_sub = pd.unique(df['subscriber']) # How many subscribers subscribe to this topic?
        subs_map = {sub: (df['subscriber'] == sub) &
                         (df['func'] == "rmw_take_with_info exit") for sub in all_sub}
        all_pid = pd.unique(df_recv['pid'])
        pid_maps = {pid: (df_recv['pid'] == pid) &
                         (df_recv['func'] == "rmw_wait exit") for pid in all_pid}
        modified_rows = []
        for idx, add_recvchange_call in add_recvchange_calls.iterrows():
            ts = add_recvchange_call['ts']
            subaddr = add_recvchange_call.at['subscriber']
            seqnum = add_recvchange_call.at['seqnum']

            # Consider missing `rmw_take_with_info exit` here
            try:
                rmw_take_idx = df.loc[(df['ts'] > ts) & subs_map[subaddr]]['ts'].idxmin()

                if 'cyclonedds' in df['layer'].unique():
                    free_sample = df.loc[(df['func'] == 'free_sample') & (df['seqnum'] == seqnum)]
                    if len(free_sample) == 0:
                        continue
                    free_sample = free_sample.iloc[0]
                    if free_sample['ts'] > df.at[rmw_take_idx, 'ts']:
                        rmw_take_idx = df.loc[(df['ts'] > free_sample['ts']) & subs_map[subaddr]]['ts'].idxmin()
                # if 'cyclonedds' in df['layer'].unique():
                #     free_sample = df_recv.loc[(df_recv['ts'] > ts) &
                #                               (df_recv['func'] == 'free_sample') &
                #                               (df_recv['pid'] == df.at[rmw_take_idx, 'pid']) &
                #                               (df_recv['seqnum'] == seqnum)]
                #     free_sample_idx = free_sample.idxmax()
                #     if len(free_sample) == 0:
                #         rmw_take_idx = df.loc[(df['ts'] > free_sample['ts']) & subs_map[subaddr]]['ts'].idxmin()
                #         print(df.loc[rmw_take_idx])
                    # free_sample() should be called in rmw_take, therefore
                    # free_sample() happened before rmw_take_with_info returns

                df_recv_partial.at[rmw_take_idx, 'seqnum'] = seqnum

                # TODO: Group by ip port in cls_ingress
                UDPResourceReceive_idx = df.loc[(df['ts'] < ts) &
                                                (df['func'] == 'UDPResourceReceive exit') &
                                                (df['pid'] == add_recvchange_call.at['pid'])]['ts'].idxmax()
                df_recv_partial.at[UDPResourceReceive_idx, 'seqnum'] = seqnum
            except ValueError as e:
                pass
            try:
                # Group rmw_wait exit
                pid = df_recv_partial.at[rmw_take_idx, 'pid']
                rmw_wait_idx = df_recv.loc[(df_recv['ts'] < df_recv_partial.at[rmw_take_idx,'ts']) &
                                            pid_maps[pid]]['ts'].idxmax()
                modified_row = df_recv.loc[rmw_wait_idx]
                modified_row.at['seqnum'] = add_recvchange_call.at['seqnum']
                modified_row.at['guid'] = guid
                modified_rows.append(modified_row)
            except ValueError as e:
                pass
        # Doesn't need to remove duplicates for
        # a = pd.DataFrame(modified_rows)
        # print(a[~a.index.duplicated(keep='first')])
        df_recv_partial = pd.concat([df_recv_partial, pd.DataFrame(modified_rows)])

        # Merge all modified dataframes
        df_merged = df_send_partial.append(df_recv_partial, ignore_index=True, sort=False)

        # handle other log files
        for other_log in other_log_list:
            df_other = other_log[guid]
            df_merged = df_merged.append(df_other, ignore_index=True, sort=False)

        # Avoid `TypeError: boolean value of NA is ambiguous` when calling groupby()
        df_merged['subscriber'] = df_merged['subscriber'].fillna(np.nan)
        df_merged['guid'] = df_merged['guid'].fillna(np.nan)
        df_merged['seqnum'] = df_merged['seqnum'].fillna(np.nan)
        df_merged.sort_values(by=['ts'], inplace=True)
        gb_merged = df_merged.groupby(['guid', 'seqnum'])

        ros_msgs = {msg_id:log for msg_id, log in gb_merged} # msg_id: (guid, seqnum)
        # get topic name from log
        topic_name = df_merged['topic_name'].dropna().unique()
        if len(topic_name) > 1:
            raise Exception("More than one topic in a log file")
        topic_name = topic_name[0]

        if topic_name in res:
            res[topic_name] = {**res[topic_name], **ros_msgs}
        else:
            res[topic_name] = ros_msgs
        print('finished parsing ' + topic_name)
    return res

def extract_individual_rosmsg2(df_send, df_recv, df_cls):
#     unix_time_off = statistics.median(sofa_time.get_unix_mono_diff() for i in range(100))
#     for df in (df_send, df_recv, df_cls):
#         df['ts'] = df['ts'] + unix_time_off

    df_send.sort_values(by=['ts'], ignore_index=True)
    df_recv.sort_values(by=['ts'], ignore_index=True)
    df_cls.sort_values(by=['ts'], ignore_index=True)

    # publish side
    gb_send = df_send.groupby('guid')
    all_publishers_log = {guid:log for guid, log in gb_send}

    # subscription side
    gb_recv = df_recv.groupby('guid')
    all_subscriptions_log = {guid:log for guid, log in gb_recv}

    # in kernel (probably not need it)
    gb_cls = df_cls.groupby('guid')
    all_cls_log = {guid:log for guid, log in gb_cls}

    interested_guids = all_subscriptions_log.keys() \
                     & all_publishers_log.keys()

    res = {}
    for guid in interested_guids:
        # get a publisher from log
        df = all_publishers_log[guid].copy()
        df_send_partial = all_publishers_log[guid].copy()
        add_data_calls = df[~pd.isna(df['seqnum'])] # get all non-NaN seqnums in log
        try:
            pubaddr, = pd.unique(df['publisher']).dropna()
        except ValueError as e:
            print('Find a guid that is not associated with a publisher memory address. Error: ' + str(e))
            continue
        print(pubaddr)

        modified_rows = []
        for idx, add_data_call in add_data_calls.iterrows():
            seqnum = add_data_call['seqnum']
            ts = add_data_call['ts']

            rcl_idx = df.loc[(df['ts'] < ts) & (df['layer'] == 'rcl')]['ts'].idxmax()
            df_send_partial.loc[rcl_idx, 'seqnum'] = add_data_call.loc['seqnum']

            # Use the two timestamps to get a slice of dataframe
            # Here we drop :~RTPSMessageGroup exit"
            ts_cls = df_cls[(df_cls['guid']   == guid) &
                            (df_cls['seqnum'] == seqnum) &
                            (df_cls['layer']  == 'cls_egress')]['ts'].max() # Get ts upper bound
            df_send_tgt = df_send[(ts <= df_send['ts']) &
                                  (df_send['ts'] <= ts_cls) &
                                  (df_send['publisher'] == pubaddr)]
            modified_row = df_send_tgt.copy()
            modified_row['guid'] = guid
            modified_row['seqnum'] = seqnum
            modified_rows.append(modified_row)
        df_send_partial = df_send_partial.combine_first(pd.concat(modified_rows))

        # get a subscrption from log
        df = all_subscriptions_log[guid].copy()
        df_recv_partial = all_subscriptions_log[guid].copy()
        add_recvchange_calls = df[~pd.isna(df['seqnum'])] # get all not nan seqnums in log

        all_sub = pd.unique(df['subscriber']) # How many subscribers subscribe to this topic?
        subs_map = {sub: (df['subscriber'] == sub) &
                         (df['func'] == "rmw_take_with_info exit") for sub in all_sub}
        all_pid = pd.unique(df_recv['pid'])
        pid_maps = {pid: (df_recv['pid'] == pid) &
                         (df_recv['func'] == "rmw_wait exit") for pid in all_pid}
        modified_rows = []
        for idx, add_recvchange_call in add_recvchange_calls.iterrows():
            ts = add_recvchange_call.at['ts']
            subaddr = add_recvchange_call.at['subscriber']
            seqnum = add_recvchange_call.at['seqnum']

            # Use the two timestamps to get a slice of dataframe
            ts_cls = df_cls[(df_cls['guid']   == guid) &
                            (df_cls['seqnum'] == seqnum) &
                            (df_cls['layer']  == 'cls_ingress')]['ts'].min()
            df_recv_tgt = df_recv[(ts_cls < df_recv['ts']) & (df_recv['ts'] < ts)].copy()
            # Consider missing `rmw_take_with_info exit` here
            try:
                rmw_take_idx = df.loc[(df['ts'] > ts) & subs_map[subaddr]]['ts'].idxmin()
                df_recv_partial.at[rmw_take_idx, 'seqnum'] = seqnum

                # TODO: Group by ip port in cls_ingress
                UDPResourceReceive_idx = df_recv_tgt.loc[(df_recv_tgt['func'] == 'UDPResourceReceive exit') &
                                                         (df_recv_tgt['pid'] == add_recvchange_call.at['pid'])]['ts'].idxmax();
                df_recv_partial.at[UDPResourceReceive_idx, 'seqnum'] = seqnum

                # Group rmw_wait exit
                pid = df_recv_partial.at[rmw_take_idx, 'pid']
                rmw_wait_idx = df_recv.loc[(df_recv['ts'] < df_recv_partial.at[rmw_take_idx,'ts']) &
                                            pid_maps[pid]]['ts'].idxmax()
                modified_row = df_recv.loc[rmw_wait_idx]
                modified_row.at['seqnum'] = add_recvchange_call.at['seqnum']
                modified_row.at['guid'] = guid
                modified_rows.append(modified_row)
            except ValueError as e:
                pass
        df_recv_partial = pd.concat([df_recv_partial, pd.DataFrame(modified_rows)])

        # Merge all modified dataframes
        df_merged = df_send_partial.append(df_recv_partial, ignore_index=True, sort=False)

        # handle other log files
        df_merged = df_merged.append(df_cls, ignore_index=True, sort=False)

        # Avoid `TypeError: boolean value of NA is ambiguous` when calling groupby()
        df_merged['subscriber'] = df_merged['subscriber'].fillna(np.nan)
        df_merged['guid'] = df_merged['guid'].fillna(np.nan)
        df_merged['seqnum'] = df_merged['seqnum'].fillna(np.nan)
        df_merged.sort_values(by=['ts'], inplace=True)
        gb_merged = df_merged.groupby(['guid', 'seqnum'])

        ros_msgs = {msg_id:log for msg_id, log in gb_merged} # msg_id: (guid, seqnum)

        # get topic name from log
        topic_name = df_merged['topic_name'].dropna().unique()
        if len(topic_name) > 1:
            raise Exception("More than one topic in a log file")
        topic_name = topic_name[0]
        if res.get(topic_name) is None:
            res[topic_name] = ros_msgs
        else:
            res[topic_name].update(ros_msgs)
        print(type(res[topic_name]))
        print('finished parsing ' + topic_name)

    return res
        # print(df_recv_partial[['layer', 'ts', 'func', 'guid', 'seqnum']])

def print_all_msgs(res):
    for topic_name, all_msgs_log in res.items():
        print('topic: ' + topic_name)
        for (guid, seqnum), msg_log in all_msgs_log.items():
            print('msg_id: ', (guid, seqnum))
            print(msg_log)
            print('')

def get_rcl_publish(df):
    try:
        rcl = df.loc[df['func'] == 'rcl_publish'].iloc[0] # shuold be unique
    except ValueError as e:
        print(e)
        return pd.Series('false', index=['layer']) # return a dummy for easy checkup
    return rcl

# @profile
def ros_msgs_trace_read(items, cfg):
    sofa_ros2_fieldnames = [
        "timestamp",  # 0
        "event",      # 1
        "duration",   # 2
        "deviceId",   # 3
        "copyKind",   # 4
        "payload",    # 5
        "bandwidth",  # 6
        "pkt_src",    # 7
        "pkt_dst",    # 8
        "pid",        # 9
        "tid",        # 10
        "name",       # 11
        "category",   # 12
        "unit",
        "msg_id"]

    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        # msg_log['subscriber'] = msg_log['subscriber'].apply(lambda x: np.nan if x is pd.NA else x)
        gb_sub = msg_log.groupby('subscriber') # How many subscribers receviced this ros message?
        start = get_rcl_publish(msg_log)
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        for sub_addr, sub_log in gb_sub:
            trace = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))
            end = sub_log.iloc[-1]
            if end.at['layer'] != 'rmw': # skip when the last function call is not from rmw (eg. rosbag2)
                continue

            time = start['ts']
            if cfg is not None and not cfg.absolute_timestamp:
                time = start['ts'] - cfg.time_base
            trace['timestamp'] = time
            trace['duration'] = (end['ts'] - start['ts']) * 1e3 # ms
            trace['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s<br>Transmission: %s -> %s<br>Seqnum: %d" % \
                            (start['layer'], start['func'], end['layer'], end['func'],
                             start['topic_name'],
                             start['comm'], end['comm'],
                             int(start['seqnum']))
            trace['unit'] = 'ms'
            trace['msg_id'] = msg_id
            traces.append(trace)
    traces = pd.DataFrame(traces)
    return traces

def ros_msgs_trace_read_ros_lat_send(items, cfg):
    sofa_ros2_fieldnames = [
        "timestamp",  # 0
        "event",      # 1
        "duration",   # 2
        "deviceId",   # 3
        "copyKind",   # 4
        "payload",    # 5
        "bandwidth",  # 6
        "pkt_src",    # 7
        "pkt_dst",    # 8
        "pid",        # 9
        "tid",        # 10
        "name",       # 11
        "category",   # 12
        "unit",
        "msg_id"]

    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        trace = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))
        start = get_rcl_publish(msg_log)
        end = msg_log.loc[msg_log['func'] == 'write_sample_gc'].iloc[0]
        time = end['ts']
        if cfg is not None and not cfg.absolute_timestamp:
            time = end['ts'] - cfg.time_base
        trace['timestamp'] = time
        trace['duration'] = (end['ts'] - start['ts']) * 1e3 # ms
        trace['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s<br>Transmission: %s -> %s<br>Seqnum: %d" % \
                            (start['layer'], start['func'], end['layer'], end['func'],
                             start['topic_name'],
                             start['comm'], end['comm'],
                             int(start['seqnum']))
        trace['unit'] = 'ms'
        trace['msg_id'] = msg_id
        traces.append(trace)
    traces = pd.DataFrame(traces)
    return traces

def ros_msgs_trace_read_os_lat_send(items, cfg):
    sofa_ros2_fieldnames = [
        "timestamp",  # 0
        "event",      # 1
        "duration",   # 2
        "deviceId",   # 3
        "copyKind",   # 4
        "payload",    # 5
        "bandwidth",  # 6
        "pkt_src",    # 7
        "pkt_dst",    # 8
        "pid",        # 9
        "tid",        # 10
        "name",       # 11
        "category",   # 12
        "unit",
        "msg_id"]

    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        start = get_rcl_publish(msg_log)
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        all_sendSync = msg_log.loc[(msg_log['func'] == 'sendSync') | (msg_log['func'] == 'nn_xpack_send1')].copy()
        all_egress = msg_log.loc[msg_log['layer'] == 'cls_egress']

        for _, sendSync in all_sendSync.iterrows():
            trace = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))
            # addr = sendSync['daddr']
            port = sendSync['dport']
            egress = all_egress.loc[(all_egress['dport'] == port)].iloc[0]

            time = sendSync['ts']
            if cfg is not None and not cfg.absolute_timestamp:
                time = sendSync['ts'] - cfg.time_base
            trace['timestamp'] = time
            trace['duration'] = (egress['ts'] - sendSync['ts']) * 1e3 # ms
            trace['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s<br>Destination address: %s:%d" % \
                            (sendSync['layer'], sendSync['func'], egress['layer'], '',
                             start['topic_name'],
                             str(ipaddress.IPv4Address(socket.ntohl(int(all_egress['daddr'].unique())))),
                             socket.ntohs(int(port)))
            trace['unit'] = 'ms'
            trace['msg_id'] = msg_id
            traces.append(trace)
    traces = pd.DataFrame(traces)
    return traces

def ros_msgs_trace_read_os_lat_recv(items, cfg):
    sofa_ros2_fieldnames = [
        "timestamp",  # 0
        "event",      # 1
        "duration",   # 2
        "deviceId",   # 3
        "copyKind",   # 4
        "payload",    # 5
        "bandwidth",  # 6
        "pkt_src",    # 7
        "pkt_dst",    # 8
        "pid",        # 9
        "tid",        # 10
        "name",       # 11
        "category",   # 12
        "unit",
        "msg_id"]

    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        start = get_rcl_publish(msg_log)
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        all_recv = msg_log.loc[(msg_log['func'] == 'UDPResourceReceive exit') |
                               (msg_log['func'] == 'ddsi_udp_conn_read exit')].copy()
        all_ingress = msg_log.loc[msg_log['layer'] == 'cls_ingress'].copy()

        for _, ingress in all_ingress.iterrows():
            trace = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))
            addr = ingress['daddr']
            port = ingress['dport']
            try:
                recv = all_recv.loc[(all_recv['dport'] == port)].iloc[0]
            except Exception as e:
                print(str(msg_id) + " missing " + str(port))
                print(e)
                continue

            time = ingress['ts']
            if cfg is not None and not cfg.absolute_timestamp:
                time = ingress['ts'] - cfg.time_base
            trace['timestamp'] = time
            trace['duration'] = (recv['ts'] - ingress['ts']) * 1e3 # ms
            trace['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s<br>Source address: %s:%d, Destination address: %s:%d<br>Seqnum: %d" % \
                (ingress['layer'], '', recv['layer'], recv['func'], start['topic_name'],
                str(ipaddress.IPv4Address(socket.ntohl(int(ingress['saddr'])))), socket.ntohs(int(ingress['sport'])),
                str(ipaddress.IPv4Address(socket.ntohl(int(addr)))), socket.ntohs(int(port)),
                int(ingress['seqnum']))
            trace['unit'] = 'ms'
            trace['msg_id'] = msg_id
            traces.append(trace)
    traces = pd.DataFrame(traces)
    return traces

def ros_msgs_trace_read_dds_lat_send(items, cfg):
    sofa_ros2_fieldnames = [
        "timestamp",  # 0
        "event",      # 1
        "duration",   # 2
        "deviceId",   # 3
        "copyKind",   # 4
        "payload",    # 5
        "bandwidth",  # 6
        "pkt_src",    # 7
        "pkt_dst",    # 8
        "pid",        # 9
        "tid",        # 10
        "name",       # 11
        "category",   # 12
        "unit",
        "msg_id"]

    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        start = get_rcl_publish(msg_log)
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        all_sendSync = msg_log.loc[(msg_log['func'] == 'sendSync') | (msg_log['func'] == 'nn_xpack_send1')].copy()
        add_pub_change = msg_log.loc[(msg_log['func'] == 'add_pub_change') |
                                     (msg_log['func'] == 'write_sample_gc')].copy().squeeze()

        for _, sendSync in all_sendSync.iterrows():
            trace = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))

            time = add_pub_change['ts']
            if cfg is not None and not cfg.absolute_timestamp:
                time = add_pub_change['ts'] - cfg.time_base
            trace['timestamp'] = time
            trace['duration'] = (sendSync['ts'] - add_pub_change['ts']) * 1e3 # ms
            trace['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s" % \
                            (add_pub_change['layer'], add_pub_change['func'], sendSync['layer'], sendSync['func'], \
                             start['topic_name'])
            trace['unit'] = 'ms'
            trace['msg_id'] = msg_id
            traces.append(trace)
    traces = pd.DataFrame(traces)
    return traces

def ros_msgs_trace_read_dds_ros_lat_recv(items, cfg):
    sofa_ros2_fieldnames = [
        "timestamp",  # 0
        "event",      # 1
        "duration",   # 2
        "deviceId",   # 3
        "copyKind",   # 4
        "payload",    # 5
        "bandwidth",  # 6
        "pkt_src",    # 7
        "pkt_dst",    # 8
        "pid",        # 9
        "tid",        # 10
        "name",       # 11
        "category",   # 12
        "unit",
        "msg_id"]

    traces_indds = []
    traces_inros = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        # msg_log['subscriber'] = msg_log['subscriber'].apply(lambda x: np.nan if x is pd.NA else x)
        gb_sub = msg_log.groupby('subscriber') # How many subscribers receviced this ros message?
        start = get_rcl_publish(msg_log)
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        for sub_addr, sub_log in gb_sub:
            trace_indds = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))
            trace_inros = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))
            end = sub_log.iloc[-1]
            if end.at['layer'] != 'rmw': # skip when the last function call is not from rmw (eg. rosbag2)
                continue

            try:
                pid_ros, = sub_log.loc[sub_log['func'] == 'rmw_take_with_info exit', 'pid'].unique() # shuold be unique
                pid_dds, = sub_log.loc[(sub_log['func'] == 'add_received_change') |
                                       (msg_log['func'] == 'ddsi_udp_conn_read exit'), 'pid'].unique()
            except ValueError as e:
                print(e)
                continue

            try: # Consider missing 'rmw_wait exit' here
                os_return = msg_log.loc[((msg_log['func'] == 'UDPResourceReceive exit') | (msg_log['func'] == 'ddsi_udp_conn_read exit')) &
                                         (msg_log['pid'] == pid_dds)].iloc[0]
                dds_return = msg_log.loc[(msg_log['func'] == 'rmw_wait exit') & (msg_log['pid'] == pid_ros)].iloc[0]
                ros_return = sub_log.loc[sub_log['func'] == 'rmw_take_with_info exit'].squeeze()
            except IndexError as e:
                print(e)
                continue

            time = os_return['ts']
            if cfg is not None and not cfg.absolute_timestamp:
                time = os_return['ts'] - cfg.time_base
            trace_indds['timestamp'] = time
            trace_indds['duration'] = (dds_return['ts'] - os_return['ts']) * 1e3 # ms
            trace_indds['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s<br>Transmission: %s -> %s" % \
                (os_return['layer'], os_return['func'], dds_return['layer'], dds_return['func'],
                 start['topic_name'], start['comm'], os_return['comm'])
            trace_indds['unit'] = 'ms'
            trace_indds['msg_id'] = msg_id

            time = dds_return['ts']
            if cfg is not None and not cfg.absolute_timestamp:
                time = dds_return['ts'] - cfg.time_base
            trace_inros['timestamp'] = time
            trace_inros['duration'] = (ros_return['ts'] - dds_return['ts']) * 1e3 # ms
            trace_inros['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s<br>Transmission: %s -> %s" % \
                (dds_return['layer'], dds_return['func'], ros_return['layer'], ros_return['func'],
                 start['topic_name'], start['comm'], ros_return['comm'])
            trace_inros['unit'] = 'ms'
            trace_inros['msg_id'] = msg_id

            if trace_inros['duration'] <= 0 or trace_indds['duration'] <= 0:
                continue

            traces_indds.append(trace_indds)
            traces_inros.append(trace_inros)
    traces_dds = pd.DataFrame(traces_indds)
    traces_ros = pd.DataFrame(traces_inros)
    return (traces_dds, traces_ros)

def ros_msgs_trace_read_NET_real(items, cfg):
    sofa_ros2_fieldnames = [
        "timestamp",  # 0
        "event",      # 1
        "duration",   # 2
        "deviceId",   # 3
        "copyKind",   # 4
        "payload",    # 5
        "bandwidth",  # 6
        "pkt_src",    # 7
        "pkt_dst",    # 8
        "pid",        # 9
        "tid",        # 10
        "name",       # 11
        "category",   # 12
        "unit",
        "msg_id"]

    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        start = msg_log.iloc[0]
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        for _, sendSync in all_sendSync.iterrows():
            trace = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))

            time = add_pub_change['ts']
            if cfg is not None and not cfg.absolute_timestamp:
                time = add_pub_change['ts'] - cfg.time_base
            trace['timestamp'] = time
            trace['duration'] = (sendSync['ts'] - time) * 1e3 # ms
            trace['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s" % \
                            (add_pub_change['layer'], add_pub_change['func'], sendSync['layer'], sendSync['func'], \
                             start['topic_name'])
            trace['unit'] = 'ms'
            traces.append(trace)
    traces = pd.DataFrame(traces)
    return traces

def find_outliers(all_traces, filt):
    """find outliers base on filt"""
    mean = filt.data['duration'].mean()
    std = filt.data['duration'].std()
    thres = mean + std * 3
    targets_trace = pd.DataFrame(columns=sofa_ros2_fieldnames)
    targets = filt.data.loc[filt.data['duration'] > thres, 'msg_id']
    for idx, msg_id in targets.iteritems():
        pts = []
        for trace in all_traces:
            tgt = trace.data[trace.data['msg_id'] == msg_id]
            pts.append(tgt)
        targets_trace = pd.concat([targets_trace, *pts])
    # print(targets_trace)
    sofatrace_targets = sofa_models.SOFATrace()
    sofatrace_targets.name = 'outliers'
    sofatrace_targets.title = 'outliers'
    sofatrace_targets.color = 'Black'
    sofatrace_targets.x_field = 'timestamp'
    sofatrace_targets.y_field = 'duration'
    sofatrace_targets.data = targets_trace
    return sofatrace_targets

def find_retransmissions(items, cfg):
    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        start = get_rcl_publish(msg_log)
        end = msg_log.iloc[-1]
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        all_egress = msg_log.loc[msg_log['layer'] == 'cls_egress']
        msgids = all_egress['msg_id'].unique()
        if 'DATA' in msgids and len(msgids) == 1 and len(all_egress) > 1:
            first_xmit = all_egress.iloc[0]
            for _, egress in all_egress.iloc[1:].iterrows():
                trace = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))

                time = egress['ts']
                if cfg is not None and not cfg.absolute_timestamp:
                    time = egress['ts'] - cfg.time_base
                trace['timestamp'] = time
                trace['duration'] = (egress['ts'] - first_xmit['ts']) * 1e3 # ms
                trace['name'] = ("Retransmission (time to first transmission)<br>" + \
                                 "Topic Name: %s<br>Transmission: %s -> %s<br>Seqnum: %d") % \
                                (start['topic_name'], start['comm'], end['comm'], int(start['seqnum']))
                trace['unit'] = 'ms'
                trace['msg_id'] = msg_id
                traces.append(trace)
                print(trace)
    return pd.DataFrame(traces)

def find_sample_drop(items, cfg):
    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        start = get_rcl_publish(msg_log)
        end = msg_log.iloc[-1]
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        if 'cyclonedds' in msg_log['layer'].unique():
            free_sample = msg_log[(msg_log['func'] == 'free_sample')] # TODO: change to 'rmw_take_with_info exit'
            receive = msg_log[(msg_log['func'] == 'ddsi_udp_conn_read exit')]
            if len(free_sample) == 1 or len(receive) == 0:
                continue
        else:
            continue
        trace = dict(zip(sofa_ros2_fieldnames, itertools.repeat(-1)))

        time = msg_log.iloc[-1]['ts']
        if cfg is not None and not cfg.absolute_timestamp:
            time = msg_log.iloc[-1]['ts'] - cfg.time_base
        trace['timestamp'] = time
        trace['duration'] = 1 # drop one sample
        trace['name'] = "Sample drop<br>Topic Name: %s<br>Transmission: %s -> %s<br>Seqnum: %d" % \
                        ( start['topic_name'], start['comm'], end['comm'], int(start['seqnum']))
        trace['unit'] = ''
        trace['msg_id'] = msg_id
        traces.append(trace)
        print(trace)
    return pd.DataFrame(traces)

def run(cfg, cfg2=None):
    """ Start preprocessing. """
    # Read all log files generated by ebpf_ros2_*
    # TODO: convert addr and port to uint32, uint16
    read_csv = functools.partial(pd.read_csv, dtype={
        'pid':'Int32', 'seqnum':'Int64', 'subscriber':'Int64', 'publisher':'Int64'})

    send_log = 'send_log.csv'
    recv_log = 'recv_log.csv'
    other_log = ['cls_bpf_log.csv']

    if cfg is None:
        cfg = sofa_config.SOFA_Config()
    elif cfg2 is None:
        cfg2 = cfg

    with open(os.path.join(cfg.logdir, 'unix_time_off.txt')) as f:
        lines = f.readlines()
        cfg.unix_time_off = float(lines[0])
        sofa_print.print_hint('unix time offset:' + str(cfg.unix_time_off) + ' in ' + cfg.logdir)

    with open(os.path.join(cfg2.logdir, 'unix_time_off.txt')) as f:
        lines = f.readlines()
        cfg.unix_time_off = float(lines[0])
        sofa_print.print_hint('unix time offset:' + str(cfg2.unix_time_off) + ' in ' + cfg2.logdir)

    send_log = os.path.join(cfg.logdir, cfg.ros2logdir, 'send_log.csv')
    recv_log = os.path.join(cfg2.logdir, cfg2.ros2logdir, 'recv_log.csv')
    print(send_log, recv_log)
    cvs_files_others = []
    for idx in range(len(other_log)):
        print(os.path.join(cfg.logdir, cfg.ros2logdir, other_log[idx]))
        cvs_files_others.append(
            (cfg, os.path.join(cfg.logdir, cfg.ros2logdir, other_log[idx])))
    if cfg2 is not cfg:
        for idx in range(len(other_log)):
            print(os.path.join(cfg2.logdir, cfg2.ros2logdir, other_log[idx]))
            cvs_files_others.append(
                (cfg2, os.path.join(cfg2.logdir, cfg2.ros2logdir, other_log[idx]))
            )

    df_send = (cfg, read_csv(send_log))
    df_recv = (cfg2, read_csv(recv_log))
    df_others = []
    for cfg_to_pass, csv_file in cvs_files_others:
        try:
            df_others.append((cfg_to_pass, read_csv(csv_file)))
        except pd.errors.EmptyDataError as e:
            print(csv_file + ' is empty')

    all_msgs = extract_individual_rosmsg(df_send, df_recv, *df_others)
    print(all_msgs)

    # TODO: Filiter topics

    # Calculate ros latency for all topics
    res = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(ros_msgs_trace_read, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            topic = item[0]
            res.append(future.result())
    print(res)
    # res = ros_msgs_trace_read(next(iter(all_msgs.items())), cfg=cfg)

    ros_lat_send = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(ros_msgs_trace_read_ros_lat_send, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            topic = item[0]
            ros_lat_send.append(future.result())
    print(ros_lat_send)

    # Calculate time spent in OS for all topics
    os_lat_send = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(ros_msgs_trace_read_os_lat_send, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            topic = item[0]
            os_lat_send.append(future.result())
    print(os_lat_send)

    dds_lat_send = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(ros_msgs_trace_read_dds_lat_send, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            topic = item[0]
            dds_lat_send.append(future.result())
    print(dds_lat_send)

    os_lat_recv = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(ros_msgs_trace_read_os_lat_recv, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            topic = item[0]
            os_lat_recv.append(future.result())
    print(os_lat_recv)

    dds_lat_recv = []
    ros_executor_recv = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(ros_msgs_trace_read_dds_ros_lat_recv, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            print(future.result())
            # topic = item[0]
            dds_lat_recv.append(future.result()[0])
            ros_executor_recv.append(future.result()[1])
    print(dds_lat_recv)
    print(ros_executor_recv)

    retransmissions = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(find_retransmissions, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            topic = item[0]
            retransmissions.append(future.result())
    print(retransmissions)

    sample_drop = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(find_sample_drop, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            topic = item[0]
            sample_drop.append(future.result())
    print(sample_drop)

    sofatrace = sofa_models.SOFATrace()
    sofatrace.name = 'ros2_latency'
    sofatrace.title = 'ros2_latency'
    sofatrace.color = 'DeepPink'
    sofatrace.x_field = 'timestamp'
    sofatrace.y_field = 'duration'
    sofatrace.data = pd.concat(res) # TODO:

    sofatrace_ros_lat_send = sofa_models.SOFATrace()
    sofatrace_ros_lat_send.name = 'ros2_lat_send'
    sofatrace_ros_lat_send.title = 'ros2_lat_send'
    sofatrace_ros_lat_send.color = '#D15817'
    sofatrace_ros_lat_send.x_field = 'timestamp'
    sofatrace_ros_lat_send.y_field = 'duration'
    sofatrace_ros_lat_send.data = pd.concat(ros_lat_send)

    sofatrace_ros_executor_recv = sofa_models.SOFATrace()
    sofatrace_ros_executor_recv.name = 'ros2_executor_recv'
    sofatrace_ros_executor_recv.title = 'ros2_executor_recv'
    sofatrace_ros_executor_recv.color = next(color_recv)
    sofatrace_ros_executor_recv.x_field = 'timestamp'
    sofatrace_ros_executor_recv.y_field = 'duration'
    sofatrace_ros_executor_recv.data = pd.concat(ros_executor_recv)

    sofatrace_dds_lat_send = sofa_models.SOFATrace()
    sofatrace_dds_lat_send.name = 'dds_send_latency'
    sofatrace_dds_lat_send.title = 'dds_send_latency'
    sofatrace_dds_lat_send.color = next(color_send)
    sofatrace_dds_lat_send.x_field = 'timestamp'
    sofatrace_dds_lat_send.y_field = 'duration'
    sofatrace_dds_lat_send.data = pd.concat(dds_lat_send)

    sofatrace_dds_lat_recv = sofa_models.SOFATrace()
    sofatrace_dds_lat_recv.name = 'dds_recv_latency'
    sofatrace_dds_lat_recv.title = 'dds_recv_latency'
    sofatrace_dds_lat_recv.color = next(color_recv)
    sofatrace_dds_lat_recv.x_field = 'timestamp'
    sofatrace_dds_lat_recv.y_field = 'duration'
    sofatrace_dds_lat_recv.data = pd.concat(dds_lat_recv)

    sofatrace_os_lat_send = sofa_models.SOFATrace()
    sofatrace_os_lat_send.name = 'os_send_latency'
    sofatrace_os_lat_send.title = 'os_send_latency'
    sofatrace_os_lat_send.color = next(color_send)
    sofatrace_os_lat_send.x_field = 'timestamp'
    sofatrace_os_lat_send.y_field = 'duration'
    sofatrace_os_lat_send.data = pd.concat(os_lat_send)

    sofatrace_os_lat_recv = sofa_models.SOFATrace()
    sofatrace_os_lat_recv.name = 'os_recv_latency'
    sofatrace_os_lat_recv.title = 'os_recv_latency'
    sofatrace_os_lat_recv.color = next(color_recv)
    sofatrace_os_lat_recv.x_field = 'timestamp'
    sofatrace_os_lat_recv.y_field = 'duration'
    sofatrace_os_lat_recv.data = pd.concat(os_lat_recv)

    sofatrace_retransmissions = sofa_models.SOFATrace()
    sofatrace_retransmissions.name = 'retransmissions'
    sofatrace_retransmissions.title = 'retransmissions'
    sofatrace_retransmissions.color = 'Crimson'
    sofatrace_retransmissions.x_field = 'timestamp'
    sofatrace_retransmissions.y_field = 'duration'
    sofatrace_retransmissions.data = pd.concat(retransmissions)
    sofatrace_retransmissions.highlight = True

    sofatrace_sample_drop = sofa_models.SOFATrace()
    sofatrace_sample_drop.name = 'sample_drop'
    sofatrace_sample_drop.title = 'sample_drop'
    sofatrace_sample_drop.color = 'DarkCyan'
    sofatrace_sample_drop.x_field = 'timestamp'
    sofatrace_sample_drop.y_field = 'duration'
    sofatrace_sample_drop.data = pd.concat(sample_drop)
    sofatrace_sample_drop.highlight = True

    sofatrace_targets = find_outliers(
        [sofatrace, sofatrace_ros_executor_recv, \
         sofatrace_dds_lat_send, sofatrace_dds_lat_recv, \
         sofatrace_os_lat_send, sofatrace_os_lat_recv], sofatrace)

    # cmd_vel = all_msgs['/cmd_vel']
    # cmd_vel_msgids = [('1.f.c5.ba.f4.30.0.0.1.0.0.0|0.0.10.3', num) for num in [46, 125, 170, 208, 269, 329, 545, 827, 918, 1064, 1193, 1228, 1282]]
    # print(cmd_vel[('1.f.c5.ba.f4.30.0.0.1.0.0.0|0.0.10.3', 45)])
    # res2 = ros_msgs_trace_read(('/cmd_vel', {msgid:cmd_vel[msgid] for msgid in cmd_vel_msgids}), cfg=cfg)
    # highlight = sofa_models.SOFATrace()
    # highlight.name = 'update_cmd_vel'
    # highlight.title = 'Change velocity event'
    # highlight.color = next(color)
    # highlight.x_field = 'timestamp'
    # highlight.y_field = 'duration'
    # highlight.data = pd.concat([res2])

    return [sofatrace,
            sofatrace_ros_lat_send, sofatrace_ros_executor_recv,
            sofatrace_dds_lat_send, sofatrace_dds_lat_recv,
            sofatrace_os_lat_send, sofatrace_os_lat_recv,
            sofatrace_targets, sofatrace_retransmissions, sofatrace_sample_drop]

def benchmarking():
    read_csv = functools.partial(pd.read_csv, dtype={
        'pid':'Int32', 'seqnum':'Int64', 'subscriber':'Int64', 'publisher':'Int64'})
    cvs_files_others = ['cls_bpf_log.csv']
    df_send = read_csv('send_log.csv')
    df_recv = read_csv('recv_log.csv')

    try:
        all_send = df_send[(df_send['topic_name'] == '/chatter') & (df_send['func'] == 'rcl_publish')]
        guid = all_send['guid'].unique()[0]
        all_recv = df_recv[(df_recv['guid'] == guid) & (df_recv['func'] == 'rmw_take_with_info exit')]
    except Exception as e:
        all_send = df_send[df_send['comm'] == 'talker']
        all_recv = df_recv

    for ((_, send), (_, recv)) in zip(all_send.iterrows(), all_recv.iterrows()):
        print('{:.4f}'.format((recv.at['ts'] - send.at['ts']) * 1000))

def run2():
    read_csv = functools.partial(pd.read_csv, dtype={
        'pid':'Int32', 'seqnum':'Int64', 'subscriber':'Int64', 'publisher':'Int64'})
    df_send = read_csv('send_log.csv')
    df_recv = read_csv('recv_log.csv')
    df_cls1 = read_csv('cls_bpf_log.csv')
    df_cls2 = read_csv('cls_bpf_log2.csv')
    df_cls = pd.concat([df_cls1, df_cls2])

    return extract_individual_rosmsg2(df_send, df_recv, df_cls)

if __name__ == "__main__":
    # read_csv = functools.partial(pd.read_csv, dtype={'pid':'Int32', 'seqnum':'Int64'})
    # df_send = read_csv('send_log.csv')
    # df_cls = read_csv('cls_bpf_log.csv')
    # df_recv = read_csv('recv_log.csv')
    # res = extract_individual_rosmsg(df_send, df_recv, df_cls)
    # print_all_msgs(res)

    # benchmarking()
    # ./sofa_ros2_preprocess.py | Rscript -e 'summary (as.numeric (readLines ("stdin")))'

    # Use method 1
    res = run(None)
    # for r in res:
    #     print(r.data)

    # Use method 2
    # res = run2()
    # print_all_msgs(res)

    # np.set_printoptions(precision=4)
    # a = res[0].data['duration'].to_list()
    # for lat in a:
    #     print('{:.4f}'.format(lat))