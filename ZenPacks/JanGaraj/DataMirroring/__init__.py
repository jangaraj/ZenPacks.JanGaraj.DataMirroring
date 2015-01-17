'''
 ** __init__.py - Monkey Patch for Products.ZenRRD.RRDUtil.RRDUtil
 ** Copyright (C) 2015 Jan Garaj - www.jangaraj.com
 **
 ** This code goes into the __init__.py of a ZenPack. It patches the
 ** Products.ZenRRD.RRDUtil.RRDUtil.put() method. This allows 
 ** executing custom code for every RRD update that's made.
 ** When you change a code, then zenprocess restart is required.
 ** Logs are stored in standard zenprocess log: /opt/zenoss/log/zenprocess.log    
 **
 ** This program is free software; you can redistribute it and/or modify
 ** it under the terms of the GNU General Public License as published by
 ** the Free Software Foundation; either version 2 of the License, or
 ** (at your option) any later version.
 **
 ** This program is distributed in the hope that it will be useful,
 ** but WITHOUT ANY WARRANTY; without even the implied warranty of
 ** MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 ** GNU General Public License for more details.
 **
 ** You should have received a copy of the GNU General Public License
 ** along with this program; if not, write to the Free Software
 ** Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.     
'''

from Products.ZenUtils.Utils import monkeypatch, rrd_daemon_running
import thread, threading, time, logging
from time import gmtime, strftime
log = logging.getLogger("zen.RRDUtil")

@monkeypatch('Products.ZenRRD.RRDUtil.RRDUtil')
def put(self, path, value, rrdType, rrdCommand=None, cycleTime=None,
         min='U', max='U', useRRDDaemon=True, timestamp='N', start=None,
         allowStaleDatapoint=True):         
    """
    Save the value provided in the command to the RRD file specified in path.

    If the RRD file does not exist, use the rrdType, rrdCommand, min and
    max parameters to create the file.

    @param path: name for a datapoint in a path (eg device/component/datasource_datapoint)
    @type path: string
    @param value: value to store into the RRD file
    @type value: number
    @param rrdType: RRD data type (eg ABSOLUTE, DERIVE, COUNTER)
    @type rrdType: string
    @param rrdCommand: RRD file creation command
    @type rrdCommand: string
    @param cycleTime: length of a cycle
    @type cycleTime: number
    @param min: minimum value acceptable for this metric
    @type min: number
    @param max: maximum value acceptable for this metric
    @type max: number
    @param allowStaleDatapoint: attempt to write datapoint even if a newer datapoint has already been written
    @type allowStaleDatapoint: boolean
    @return: the parameter value converted to a number
    @rtype: number or None
    """

    # run mirror task in separated thread, so it won't block RRD update
    thread = threading.Thread(target=self.mirror, args=(path, value))
    thread.start()
    # imports and inits required for put() method
    import os
    import re
    import rrdtool
    import string    
    from Products.ZenUtils.Utils import zenPath, rrd_daemon_args, rrd_daemon_retry
    EMPTY_RRD = zenPath('perf', 'empty.rrd')    
    _UNWANTED_CHARS = ''.join(
            set(string.punctuation + string.ascii_letters) - set(['.', '-', '+', 'e'])
        )
    _LAST_RRDFILE_WRITE = {}

    # rest of original put() code - Zenoss 4.2.5
    if value is None: return None

    self.dataPoints += 1
    self.cycleDataPoints += 1

    if cycleTime is None:
        cycleTime = self.defaultCycleTime

    filename = self.performancePath(path) + '.rrd'
    if not rrdCommand:
        rrdCommand = self.defaultRrdCreateCommand
    if not os.path.exists(filename):
        log.debug("Creating new RRD file %s", filename)
        dirname = os.path.dirname(filename)
        if not os.path.exists(dirname):
            os.makedirs(dirname, 0750)

        min, max = map(_checkUndefined, (min, max))
        dataSource = 'DS:%s:%s:%d:%s:%s' % (
            'ds0', rrdType, self.getHeartbeat(cycleTime), min, max)
        args = [str(filename), "--step",
            str(self.getStep(cycleTime)),]
        if start is not None:
            args.extend(["--start", "%d" % start])
        elif timestamp != 'N':
            args.extend(["--start", str(int(timestamp) - 10)])

        args.append(str(dataSource))
        args.extend(rrdCommand.split())
        rrdtool.create(*args),

    daemon_args = rrd_daemon_args() if useRRDDaemon else tuple()

    # remove unwanted chars (this is actually pretty quick)
    value = str(value).translate(None, _UNWANTED_CHARS)

    if rrdType in ('COUNTER', 'DERIVE'):
        try:
            # cast to float first because long('100.0') will fail with a
            # ValueError
            value = long(float(value))
        except (TypeError, ValueError):
            return None
    else:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
    try:
        @rrd_daemon_retry
        def rrdtool_fn():
            return rrdtool.update(str(filename), *(daemon_args + ('%s:%s' % (timestamp, value),)))
        if timestamp == 'N' or allowStaleDatapoint:
            rrdtool_fn()
        else:
            # try to detect when the last datasample was collected
            lastTs = _LAST_RRDFILE_WRITE.get(filename, None)
            if lastTs is None:
                try:
                    lastTs = _LAST_RRDFILE_WRITE[filename] = rrdtool.last(
                        *(daemon_args + (str(filename),)))
                except Exception as ex:
                     lastTs = 0
                     log.exception("Could not determine last update to %r", filename)
            # if the current datapoint is newer than the last datapoint, then write
            if lastTs < timestamp:
                _LAST_RRDFILE_WRITE[filename] = timestamp
                if log.getEffectiveLevel() < logging.DEBUG:
                    log.debug('%s: %r, currentTs = %s, lastTs = %s', filename, value, timestamp, lastTs)
                rrdtool_fn()
            else:
                if log.getEffectiveLevel() < logging.DEBUG:
                    log.debug("ignoring write %s:%s", filename, timestamp)
                return None

        log.debug('%s: %r, @ %s', str(filename), value, timestamp)
    except rrdtool.error, err:
        # may get update errors when updating too quickly
        log.error('rrdtool reported error %s %s', err, path)

    return value

@monkeypatch('Products.ZenRRD.RRDUtil.RRDUtil')
def mirror(self, *args):
    # args example:
    # ('Devices/localhost/laLoadInt1_laLoadInt1', 3)
    # TODO implement thread execution timeout

    log.info('Mirroring thread %s starting %s' % (thread.get_ident(), args))

    start_time = time.time()
    datetime = strftime("%Y-%m-%d %H:%M:%S", gmtime())
    timestamp = int(round(time.time()))
    host = args[0].split('/')[1]
    metric = '.'.join(args[0].replace('Devices/', '').split('/')[1:])
    log.info('Mirroring - host: %s, metric: %s, value: %s' % (host, metric, args[1]))

    '''    
    # insert data into file
    mirrorFile = "/tmp/zenoss_mirrored_data.txt"
    log.debug("Mirroring data into file %s", mirrorFile)
    try:
        text_file = open(mirrorFile, "a")
        text_file.write("%s\t%s\t%s\t%s\n" % (datetime, host, metric, args[1]))
        text_file.close()
    except Exception, e:
        log.error("Mirroring data into file: %s - exception: %s", mirrorFile, e)    

    # insert data into MySQL/MariaDB - www.mysql.com/www.mariadb.org
    database = 'zenoss'
    db_host = '0.0.0.0'
    db_user = 'dbuser'
    db_password = 'dbpasswd'
    db_port = 3310
    import _mysql
    import sys
    try:
        con = _mysql.connect(host=db_host, user=db_user, passwd=db_password, port=db_port, db=database)
        query = "INSERT INTO zenoss (`insert_date`, `host`, `key`, `value`) " + \
                "VALUES (\"" + datetime + "\", \"" + host + "\", \"zenoss." + metric + "\", \"" + str(args[1]) + "\")"
        con.query(query)
        log.debug("Mirroring MySQL query: %s", query)
    except Exception, e:
        log.error("Mirroring data into database: %s - error: %s", database, e)
    finally:
        if con:
            con.close()

    # send data to Carbon (Graphite) - www.graphite.wikidot.com
    # send data to Dataloop (Carbon) - www.dataloop.io
    # send data to InfluxDB (input_plugins.graphite) - www.influxdb.com
    carbon_server = '0.0.0.0'
    carbon_port = 2003
    carbon_timeout = 10
    import socket
    message = "zenoss.%s.%s %s %d\n" % (host,metric, args[1], int(timestamp))
    log.debug("Mirroring to Carbon server (Graphite/Dataloop/InfluxDB) - message: %s", message)
    try:
        carbon = socket.socket()
        carbon.connect((carbon_server, carbon_port))
        carbon.settimeout(carbon_timeout)
        carbon.sendall(message)
    except Exception, e:
        log.error('Error while sending data to Carbon: ' + str(e))
    finally:
        carbon.close()

    # send data to Zabbix - www.zabbix.com
    # create Zabbix trapper item with relevant metric key
    zabbix_server = '0.0.0.0'
    zabbix_port = 10051
    zabbix_timeout = 10
    import socket
    import struct
    import json
    metrics_data = []
    j = json.dumps
    metrics_data.append(('\t\t{\n'
                         '\t\t\t"host":%s,\n'
                         '\t\t\t"key":%s,\n'
                         '\t\t\t"value":%s,\n'
                         '\t\t\t"clock":%s}') % (j('zenoss'), j(metric), j(args[1]), int(timestamp)))
    json_data = ('{\n'
                 '\t"request":"sender data",\n'
                 '\t"data":[\n%s]\n'
                 '}') % (',\n'.join(metrics_data))
    log.debug(json_data)
    data_len = struct.pack('<Q', len(json_data))
    packet = 'ZBXD\1' + data_len + json_data
    try:
        zabbix = socket.socket()
        zabbix.connect((zabbix_server, zabbix_port))
        zabbix.settimeout(zabbix_timeout)
        zabbix.sendall(packet)
        buf = ''
        while len(buf)<13:
            chunk = zabbix.recv(13 - len(buf))
            if not chunk:
                resp_hdr = buf
                break
            buf += chunk
        resp_hdr = buf
        if not resp_hdr.startswith('ZBXD\1') or len(resp_hdr) != 13:
            log.error('Wrong zabbix response')
        else:
            resp_body_len = struct.unpack('<Q', resp_hdr[5:])[0]
            resp_body = zabbix.recv(resp_body_len)
            resp = json.loads(resp_body)
            log.debug('Zabbix response: ' + resp.get('info'))
            if resp.get('response') != 'success':
                log.error('Got error from Zabbix: %s', resp)
    except Exception, e:
        log.error('Error while sending data to Zabbix: ' + str(e))
    finally:
        zabbix.close()

    # send data to OpenTSDB - www.opentsdb.net
    opentsdb_server = '0.0.0.0'
    opentsdb_port = 2003
    opentsdb_timeout = 10
    import socket
    message = "put %s %s %d host=%s\n" % (metric, str(timestamp), args[1], host)
    log.info("Mirroring to OpenTSDB - message: %s", message)
    try:
        opentsdb = socket.socket()
        opentsdb.connect((opentsdb_server, opentsdb_port))
        carbon.settimeout(opentsdb_timeout)
        carbon.sendall(message)
    except Exception, e:
        log.error('Error while sending data to OpenTSDB: ' + str(e))
    finally:
        opentsdb.close()

    # send data to Horizon (Skyline) - www.github.com/etsy/skyline
    # https://github.com/etsy/skyline/blob/master/utils/seed_data.py
    horizon_server = '0.0.0.0'
    horizon_port = 2025
    import socket
    try:
        horizon = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        horizon.sendto(bytes("zenoss." + host + "." + metric + " " + "str(timestamp)" + " " + "str(args[1])"), (horizon_server, horizon_port))
    except Exception, e:
        log.error('Error while sending data to Horizon (Skyline): ' + str(e))

    # send data to collectd - www.collectd.org
    # https://pythonhosted.org/collectd/
    from ZenPacks.JanGaraj.DataMirroring.lib import collectd
    collectd_server = 'localhost'
    collectd_port = 25826
    try:
        conn = collectd.Connection(collectd_host = collectd_server, collectd_port = collectd_port)
        conn.zenoss.set_exact(**{host + "." + metric: float(args[1])})
    except Exception, e:
        log.error('Error while sending data to collectd: ' + str(e))
    '''

    log.info('Mirroring thread %s finished in %s sec' % (thread.get_ident(), str(time.time() - start_time)))
