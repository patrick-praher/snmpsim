#
# This file is part of snmpsim software.
#
# Copyright (c) 2010-2019, Ilya Etingof <etingof@gmail.com>
# License: http://snmplabs.com/snmpsim/license.html
#
# SNMP Snapshot Data Recorder
#
import getopt
import os
import socket
import sys
import time
import traceback

from pyasn1 import debug as pyasn1_debug
from pyasn1.type import univ
from pysnmp import debug as pysnmp_debug
from pysnmp.carrier.asyncore.dgram import udp
from pysnmp.carrier.asyncore.dgram import udp6
from pysnmp.carrier.asyncore.dgram import unix
from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import cmdgen
from pysnmp.error import PySnmpError
from pysnmp.proto import rfc1902
from pysnmp.proto import rfc1905
from pysnmp.smi import compiler
from pysnmp.smi import view
from pysnmp.smi.rfc1902 import ObjectIdentity

from snmpsim import confdir
from snmpsim import error
from snmpsim import log
from snmpsim.record import dump
from snmpsim.record import mvc
from snmpsim.record import sap
from snmpsim.record import snmprec
from snmpsim.record import walk

AUTH_PROTOCOLS = {
    'MD5': config.usmHMACMD5AuthProtocol,
    'SHA': config.usmHMACSHAAuthProtocol,
    'SHA224': config.usmHMAC128SHA224AuthProtocol,
    'SHA256': config.usmHMAC192SHA256AuthProtocol,
    'SHA384': config.usmHMAC256SHA384AuthProtocol,
    'SHA512': config.usmHMAC384SHA512AuthProtocol,
    'NONE': config.usmNoAuthProtocol
}

PRIV_PROTOCOLS = {
    'DES': config.usmDESPrivProtocol,
    '3DES': config.usm3DESEDEPrivProtocol,
    'AES': config.usmAesCfb128Protocol,
    'AES128': config.usmAesCfb128Protocol,
    'AES192': config.usmAesCfb192Protocol,
    'AES192BLMT': config.usmAesBlumenthalCfb192Protocol,
    'AES256': config.usmAesCfb256Protocol,
    'AES256BLMT': config.usmAesBlumenthalCfb256Protocol,
    'NONE': config.usmNoPrivProtocol
}

RECORD_TYPES = {
    dump.DumpRecord.ext: dump.DumpRecord(),
    mvc.MvcRecord.ext: mvc.MvcRecord(),
    sap.SapRecord.ext: sap.SapRecord(),
    walk.WalkRecord.ext: walk.WalkRecord(),
    snmprec.SnmprecRecord.ext: snmprec.SnmprecRecord(),
    snmprec.CompressedSnmprecRecord.ext: snmprec.CompressedSnmprecRecord()
}

HELP_MESSAGE = """\
Usage: %s [--help]
    [--version]
    [--debug=<%s>]
    [--debug-asn1=<%s>]
    [--logging-method=<%s[:args>]>]
    [--log-level=<%s>]
    [--protocol-version=<1|2c|3>]
    [--community=<string>]
    [--v3-user=<username>]
    [--v3-auth-key=<key>]
    [--v3-auth-proto=<%s>]
    [--v3-priv-key=<key>]
    [--v3-priv-proto=<%s>]
    [--v3-context-engine-id=<[0x]string>]
    [--v3-context-name=<[0x]string>]
    [--use-getbulk]
    [--getbulk-repetitions=<number>]
    [--agent-udpv4-endpoint=<X.X.X.X:NNNNN>]
    [--agent-udpv6-endpoint=<[X:X:..X]:NNNNN>]
    [--agent-unix-endpoint=</path/to/named/pipe>]
    [--timeout=<seconds>] [--retries=<count>]
    [--mib-source=<url>]
    [--start-object=<MIB-NAME::[symbol-name]|OID>]
    [--stop-object=<MIB-NAME::[symbol-name]|OID>]
    [--destination-record-type=<%s>]
    [--output-file=<filename>]
    [--variation-modules-dir=<dir>]
    [--variation-module=<module>]
    [--variation-module-options=<args>]
    [--continue-on-errors=<max-sustained-errors>]""" % (
    sys.argv[0],
    '|'.join([x for x in getattr(pysnmp_debug, 'FLAG_MAP',
                                 getattr(pysnmp_debug, 'flagMap', ()))
              if x != 'mibview']),
    '|'.join([x for x in getattr(pyasn1_debug, 'FLAG_MAP',
                                 getattr(pyasn1_debug, 'flagMap', ()))]),
    '|'.join(log.METHODS_MAP),
    '|'.join(log.LEVELS_MAP),
    '|'.join(sorted([x for x in AUTH_PROTOCOLS if x != 'NONE'])),
    '|'.join(sorted([x for x in PRIV_PROTOCOLS if x != 'NONE'])),
    '|'.join(RECORD_TYPES)
)


class SnmprecRecordMixIn(object):

    def formatValue(self, oid, value, **context):
        textOid, textTag, textValue = snmprec.SnmprecRecord.formatValue(
            self, oid, value
        )

        # invoke variation module
        if context['variationModule']:
            plainOid, plainTag, plainValue = snmprec.SnmprecRecord.formatValue(
                self, oid, value, nohex=True)

            if plainTag != textTag:
                context['hextag'], context['hexvalue'] = textTag, textValue

            else:
                textTag, textValue = plainTag, plainValue

            handler = context['variationModule']['record']

            textOid, textTag, textValue = handler(
                textOid, textTag, textValue, **context)

        elif 'stopFlag' in context and context['stopFlag']:
            raise error.NoDataNotification()

        return textOid, textTag, textValue


class SnmprecRecord(SnmprecRecordMixIn, snmprec.SnmprecRecord):
    pass


RECORD_TYPES[SnmprecRecord.ext] = SnmprecRecord()


class CompressedSnmprecRecord(
        SnmprecRecordMixIn, snmprec.CompressedSnmprecRecord):
    pass


RECORD_TYPES[CompressedSnmprecRecord.ext] = CompressedSnmprecRecord()

PROGRAM_NAME = os.path.basename(sys.argv[0])


def main():
    getBulkFlag = False
    continueOnErrors = 0
    getBulkRepetitions = 25
    snmpVersion = 1
    snmpCommunity = 'public'
    v3User = None
    v3AuthKey = None
    v3PrivKey = None
    v3AuthProto = 'NONE'
    v3PrivProto = 'NONE'
    v3ContextEngineId = None
    v3Context = ''
    agentUDPv4Address = (None, 161)  # obsolete
    agentUDPv4Endpoint = None
    agentUDPv6Endpoint = None
    agentUNIXEndpoint = None
    timeout = 300  # 1/100 sec
    retryCount = 3
    startOID = univ.ObjectIdentifier('1.3.6')
    stopOID = None
    mibSources = []
    defaultMibSources = ['http://mibs.snmplabs.com/asn1/@mib@']
    dstRecordType = 'snmprec'
    outputFile = None
    loggingMethod = ['stderr']
    loggingLevel = None
    variationModuleOptions = ""
    variationModuleName = variationModule = None

    try:
        opts, params = getopt.getopt(
            sys.argv[1:], 'hv',
            ['help', 'version', 'debug=', 'debug-asn1=', 'logging-method=',
             'log-level=', 'quiet',
             'v1', 'v2c', 'v3', 'protocol-version=', 'community=',
             'v3-user=', 'v3-auth-key=', 'v3-priv-key=', 'v3-auth-proto=',
             'v3-priv-proto=',
             'context-engine-id=', 'v3-context-engine-id=',
             'context=', 'v3-context-name=',
             'use-getbulk', 'getbulk-repetitions=', 'agent-address=',
             'agent-port=',
             'agent-udpv4-endpoint=', 'agent-udpv6-endpoint=',
             'agent-unix-endpoint=', 'timeout=', 'retries=',
             'start-oid=', 'stop-oid=',
             'mib-source=',
             'start-object=', 'stop-object=',
             'destination-record-type=',
             'output-file=',
             'variation-modules-dir=', 'variation-module=',
             'variation-module-options=', 'continue-on-errors='])

    except Exception as exc:
        sys.stderr.write(
            'ERROR: %s\r\n%s\r\n' % (exc, HELP_MESSAGE))
        return 1

    if params:
        sys.stderr.write(
            'ERROR: extra arguments supplied %s\r\n'
            '%s\r\n' % (params, HELP_MESSAGE))
        return 1

    for opt in opts:
        if opt[0] == '-h' or opt[0] == '--help':
            sys.stderr.write("""\
Synopsis:
  SNMP Agents Recording tool. Queries specified Agent, stores response
  data in data files for subsequent playback by SNMP Simulation tool.
  Can store a series of recordings for a more dynamic playback.

Documentation:
  http://snmplabs.com/snmpsim/snapshotting.html
%s
""" % HELP_MESSAGE)
            return 1

        if opt[0] == '-v' or opt[0] == '--version':
            import snmpsim
            import pysnmp
            import pysmi
            import pyasn1

            sys.stderr.write("""\
SNMP Simulator version %s, written by Ilya Etingof <etingof@gmail.com>
Using foundation libraries: pysmi %s, pysnmp %s, pyasn1 %s.
Python interpreter: %s
Software documentation and support at http://snmplabs.com/snmpsim
%s
""" % (snmpsim.__version__,
           getattr(pysmi, '__version__', 'unknown'),
           getattr(pysnmp, '__version__', 'unknown'),
           getattr(pyasn1, '__version__', 'unknown'),
           sys.version, HELP_MESSAGE))
            return 1

        elif opt[0] in ('--debug', '--debug-snmp'):
            pysnmp_debug.setLogger(
                pysnmp_debug.Debug(
                    *opt[1].split(','),
                    **dict(loggerName='%s.pysnmp' % PROGRAM_NAME)))

        elif opt[0] == '--debug-asn1':
            pyasn1_debug.setLogger(
                pyasn1_debug.Debug(
                    *opt[1].split(','),
                    **dict(loggerName='%s.pyasn1' % PROGRAM_NAME)))

        elif opt[0] == '--logging-method':
            loggingMethod = opt[1].split(':')

        elif opt[0] == '--log-level':
            loggingLevel = opt[1]

        elif opt[0] == '--quiet':
            log.setLogger('snmprec', 'null', force=True)

        elif opt[0] == '--v1':
            snmpVersion = 0

        elif opt[0] == '--v2c':
            snmpVersion = 1

        elif opt[0] == '--v3':
            snmpVersion = 3

        elif opt[0] == '--protocol-version':
            if opt[1] in ('1', 'v1'):
                snmpVersion = 0

            elif opt[1] in ('2', '2c', 'v2c'):
                snmpVersion = 1

            elif opt[1] in ('3', 'v3'):
                snmpVersion = 3

            else:
                sys.stderr.write(
                    'ERROR: unknown SNMP version %s\r\n'
                    '%s\r\n' % (opt[1], HELP_MESSAGE))
                return 1

        elif opt[0] == '--community':
            snmpCommunity = opt[1]

        elif opt[0] == '--v3-user':
            v3User = opt[1]

        elif opt[0] == '--v3-auth-key':
            v3AuthKey = opt[1]

        elif opt[0] == '--v3-auth-proto':
            v3AuthProto = opt[1].upper()
            if v3AuthProto not in AUTH_PROTOCOLS:
                sys.stderr.write(
                    'ERROR: bad v3 auth protocol %s\r\n'
                    '%s\r\n' % (v3AuthProto, HELP_MESSAGE))
                return 1

        elif opt[0] == '--v3-priv-key':
            v3PrivKey = opt[1]

        elif opt[0] == '--v3-priv-proto':
            v3PrivProto = opt[1].upper()
            if v3PrivProto not in PRIV_PROTOCOLS:
                sys.stderr.write(
                    'ERROR: bad v3 privacy protocol %s\r\n'
                    '%s\r\n' % (v3PrivProto, HELP_MESSAGE))
                return 1

        elif opt[0] in ('--v3-context-engine-id', '--context-engine-id'):
            if opt[1][:2] == '0x':
                v3ContextEngineId = univ.OctetString(hexValue=opt[1][2:])

            else:
                v3ContextEngineId = univ.OctetString(opt[1])

        elif opt[0] in ('--v3-context-name', '--context'):
            if opt[1][:2] == '0x':
                v3Context = univ.OctetString(hexValue=opt[1][2:])

            else:
                v3Context = univ.OctetString(opt[1])

        elif opt[0] == '--use-getbulk':
            getBulkFlag = True

        elif opt[0] == '--getbulk-repetitions':
            getBulkRepetitions = int(opt[1])

        elif opt[0] == '--agent-address':
            agentUDPv4Address = (opt[1], agentUDPv4Address[1])

        elif opt[0] == '--agent-port':
            agentUDPv4Address = (agentUDPv4Address[0], int(opt[1]))

        elif opt[0] == '--agent-udpv4-endpoint':
            f = lambda h, p=161: (h, int(p))
            try:
                agentUDPv4Endpoint = f(*opt[1].split(':'))

            except Exception:
                sys.stderr.write(
                    'ERROR: improper IPv4/UDP endpoint %s\r\n'
                    '%s\r\n' % (opt[1], HELP_MESSAGE))
                return 1

            try:
                agentUDPv4Endpoint = socket.getaddrinfo(
                        agentUDPv4Endpoint[0],
                        agentUDPv4Endpoint[1],
                        socket.AF_INET, socket.SOCK_DGRAM,
                        socket.IPPROTO_UDP)[0][4][:2]

            except socket.gaierror:
                sys.stderr.write(
                    'ERROR: unknown hostname %s\r\n'
                    '%s\r\n' % (agentUDPv4Endpoint[0], HELP_MESSAGE))
                return 1

        elif opt[0] == '--agent-udpv6-endpoint':
            if not udp6:
                sys.stderr.write(
                    'This system does not support UDP/IP6\r\n')
                return 1

            if opt[1].find(']:') != -1 and opt[1][0] == '[':
                h, p = opt[1].split(']:')

                try:
                    agentUDPv6Endpoint = h[1:], int(p)

                except Exception:
                    sys.stderr.write(
                        'ERROR: improper IPv6/UDP endpoint %s\r\n'
                        '%s\r\n' % (opt[1], HELP_MESSAGE))
                    return 1

            elif opt[1][0] == '[' and opt[1][-1] == ']':
                agentUDPv6Endpoint = opt[1][1:-1], 161

            else:
                agentUDPv6Endpoint = opt[1], 161

            try:
                agentUDPv6Endpoint = socket.getaddrinfo(
                    agentUDPv6Endpoint[0],
                    agentUDPv6Endpoint[1],
                    socket.AF_INET6, socket.SOCK_DGRAM,
                    socket.IPPROTO_UDP)[0][4][:2]

            except socket.gaierror:
                sys.stderr.write(
                    'ERROR: unknown hostname %s\r\n'
                    '%s\r\n' % (agentUDPv6Endpoint[0], HELP_MESSAGE))
                return 1

        elif opt[0] == '--agent-unix-endpoint':
            if not unix:
                sys.stderr.write(
                    'This system does not support UNIX domain sockets\r\n')
                return 1

            agentUNIXEndpoint = opt[1]

        elif opt[0] == '--timeout':
            try:
                timeout = float(opt[1]) * 100

            except Exception:
                sys.stderr.write(
                    'ERROR: improper --timeout value %s\r\n'
                    '%s\r\n' % (opt[1], HELP_MESSAGE))
                return 1

        elif opt[0] == '--retries':
            try:
                retryCount = int(opt[1])

            except Exception:
                sys.stderr.write(
                    'ERROR: improper --retries value %s\r\n'
                    '%s\r\n' % (opt[1], HELP_MESSAGE))
                return 1

        # obsolete begin
        elif opt[0] == '--start-oid':
            startOID = univ.ObjectIdentifier(opt[1])

        elif opt[0] == '--stop-oid':
            stopOID = univ.ObjectIdentifier(opt[1])

        # obsolete end
        elif opt[0] == '--mib-source':
            mibSources.append(opt[1])

        elif opt[0] == '--start-object':
            startOID = ObjectIdentity(*opt[1].split('::', 1))

        elif opt[0] == '--stop-object':
            stopOID = ObjectIdentity(*opt[1].split('::', 1), **dict(last=True))

        if opt[0] == '--destination-record-type':
            if opt[1] not in RECORD_TYPES:
                sys.stderr.write(
                    'ERROR: unknown record type <%s> (known types are %s)\r\n%s'
                    '\r\n' % (opt[1], ', '.join(RECORD_TYPES),
                              HELP_MESSAGE))
                return 1

            dstRecordType = opt[1]

        elif opt[0] == '--output-file':
            outputFile = opt[1]

        elif opt[0] == '--variation-modules-dir':
            confdir.variation.insert(0, opt[1])

        elif opt[0] == '--variation-module':
            variationModuleName = opt[1]

        elif opt[0] == '--variation-module-options':
            variationModuleOptions = opt[1]

        elif opt[0] == '--continue-on-errors':
            try:
                continueOnErrors = int(opt[1])

            except Exception:
                sys.stderr.write(
                    'ERROR: improper --continue-on-errors retries count %s\r\n'
                    '%s\r\n' % (opt[1], HELP_MESSAGE))
                return 1

    if outputFile:
        ext = os.path.extsep + RECORD_TYPES[dstRecordType].ext

        if not outputFile.endswith(ext):
            outputFile += ext

        outputFile = RECORD_TYPES[dstRecordType].open(outputFile, 'wb')

    else:
        outputFile = sys.stdout

        if sys.version_info >= (3, 0, 0):
            # binary mode write
            outputFile = sys.stdout.buffer

        elif sys.platform == "win32":
            import msvcrt

            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

    # Catch missing params

    if not agentUDPv4Endpoint and not agentUDPv6Endpoint and not agentUNIXEndpoint:
        if agentUDPv4Address[0] is None:
            sys.stderr.write(
                'ERROR: agent endpoint address not specified\r\n'
                '%s\r\n' % HELP_MESSAGE)
            return 1

        else:
            agentUDPv4Endpoint = agentUDPv4Address

    if snmpVersion == 3:
        if v3User is None:
            sys.stderr.write(
                'ERROR: --v3-user is missing\r\n'
                '%s\r\n' % HELP_MESSAGE)
            return 1

        if v3PrivKey and not v3AuthKey:
            sys.stderr.write(
                'ERROR: --v3-auth-key is missing\r\n'
                '%s\r\n' % HELP_MESSAGE)
            return 1

        if AUTH_PROTOCOLS[v3AuthProto] == config.usmNoAuthProtocol:
            if v3AuthKey is not None:
                v3AuthProto = 'MD5'

        else:
            if v3AuthKey is None:
                sys.stderr.write(
                    'ERROR: --v3-auth-key is missing\r\n'
                    '%s\r\n' % HELP_MESSAGE)
                return 1

        if PRIV_PROTOCOLS[v3PrivProto] == config.usmNoPrivProtocol:
            if v3PrivKey is not None:
                v3PrivProto = 'DES'

        else:
            if v3PrivKey is None:
                sys.stderr.write(
                    'ERROR: --v3-priv-key is missing\r\n'
                    '%s\r\n' % HELP_MESSAGE)
                return 1

    else:
        v3ContextEngineId = None
        v3ContextName = ''

    try:
        log.setLogger(PROGRAM_NAME, *loggingMethod, force=True)

        if loggingLevel:
            log.setLevel(loggingLevel)

    except error.SnmpsimError as exc:
        sys.stderr.write(
            '%s\r\n%s\r\n' % (exc, HELP_MESSAGE))
        return 1

    if getBulkFlag and not snmpVersion:
        log.info('will be using GETNEXT with SNMPv1!')
        getBulkFlag = False

    # Load variation module

    if variationModuleName:

        for variationModulesDir in confdir.variation:
            log.info(
                'Scanning "%s" directory for variation '
                'modules...' % variationModulesDir)

            if not os.path.exists(variationModulesDir):
                log.info('Directory "%s" does not exist' % variationModulesDir)
                continue

            mod = os.path.join(variationModulesDir, variationModuleName + '.py')
            if not os.path.exists(mod):
                log.info('Variation module "%s" not found' % mod)
                continue

            ctx = {'path': mod, 'moduleContext': {}}

            try:
                if sys.version_info[0] > 2:
                    exec(compile(open(mod).read(), mod, 'exec'), ctx)

                else:
                    execfile(mod, ctx)

            except Exception as exc:
                log.error('Variation module "%s" execution failure: '
                          '%s' % (mod, exc))
                return 1

            else:
                variationModule = ctx
                log.info('Variation module "%s" loaded' % variationModuleName)
                break

        else:
            log.error('variation module "%s" not found' % variationModuleName)
            return 1

    # SNMP configuration

    snmpEngine = engine.SnmpEngine()

    if snmpVersion == 3:

        if v3PrivKey is None and v3AuthKey is None:
            secLevel = 'noAuthNoPriv'

        elif v3PrivKey is None:
            secLevel = 'authNoPriv'

        else:
            secLevel = 'authPriv'

        config.addV3User(
            snmpEngine, v3User,
            AUTH_PROTOCOLS[v3AuthProto], v3AuthKey,
            PRIV_PROTOCOLS[v3PrivProto], v3PrivKey)

        log.info(
            'SNMP version 3, Context EngineID: %s Context name: %s, SecurityName: %s, '
            'SecurityLevel: %s, Authentication key/protocol: %s/%s, Encryption '
            '(privacy) key/protocol: '
            '%s/%s' % (
                v3ContextEngineId and v3ContextEngineId.prettyPrint() or '<default>',
                v3Context and v3Context.prettyPrint() or '<default>', v3User,
                secLevel, v3AuthKey is None and '<NONE>' or v3AuthKey,
                v3AuthProto,
                v3PrivKey is None and '<NONE>' or v3PrivKey, v3PrivProto))

    else:

        v3User = 'agt'
        secLevel = 'noAuthNoPriv'

        config.addV1System(snmpEngine, v3User, snmpCommunity)

        log.info(
            'SNMP version %s, Community name: '
            '%s' % (snmpVersion == 0 and '1' or '2c', snmpCommunity))

    config.addTargetParams(snmpEngine, 'pms', v3User, secLevel, snmpVersion)

    if agentUDPv6Endpoint:
        config.addSocketTransport(
            snmpEngine, udp6.domainName,
            udp6.Udp6SocketTransport().openClientMode())

        config.addTargetAddr(
            snmpEngine, 'tgt', udp6.domainName, agentUDPv6Endpoint, 'pms',
            timeout, retryCount)

        log.info('Querying UDP/IPv6 agent at [%s]:%s' % agentUDPv6Endpoint)

    elif agentUNIXEndpoint:
        config.addSocketTransport(
            snmpEngine, unix.domainName,
            unix.UnixSocketTransport().openClientMode())

        config.addTargetAddr(
            snmpEngine, 'tgt', unix.domainName, agentUNIXEndpoint, 'pms',
            timeout, retryCount)

        log.info('Querying UNIX named pipe agent at %s' % agentUNIXEndpoint)

    elif agentUDPv4Endpoint:
        config.addSocketTransport(
            snmpEngine, udp.domainName,
            udp.UdpSocketTransport().openClientMode())

        config.addTargetAddr(
            snmpEngine, 'tgt', udp.domainName, agentUDPv4Endpoint, 'pms',
            timeout, retryCount)

        log.info('Querying UDP/IPv4 agent at %s:%s' % agentUDPv4Endpoint)

    log.info('Agent response timeout: %.2f secs, retries: '
             '%s' % (timeout / 100, retryCount))

    if (isinstance(startOID, ObjectIdentity) or
            isinstance(stopOID, ObjectIdentity)):

        compiler.addMibCompiler(
            snmpEngine.getMibBuilder(),
            sources=mibSources or defaultMibSources)

        mibViewController = view.MibViewController(snmpEngine.getMibBuilder())

        try:
            if isinstance(startOID, ObjectIdentity):
                startOID.resolveWithMib(mibViewController)

            if isinstance(stopOID, ObjectIdentity):
                stopOID.resolveWithMib(mibViewController)

        except PySnmpError as exc:
            sys.stderr.write('ERROR: %s\r\n' % exc)
            return 1

    # Variation module initialization

    if variationModule:
        log.info('Initializing variation module...')

        for x in ('init', 'record', 'shutdown'):
            if x not in variationModule:
                log.error('missing "%s" handler at variation module '
                          '"%s"' % (x, variationModuleName))
                return 1

        try:
            handler = variationModule['init']

            handler(snmpEngine=snmpEngine, options=variationModuleOptions,
                    mode='recording', startOID=startOID, stopOID=stopOID)

        except Exception as exc:
            log.error(
                'Variation module "%s" initialization FAILED: '
                '%s' % (variationModuleName, exc))

        else:
            log.info(
                'Variation module "%s" initialization OK' % variationModuleName)

    dataFileHandler = RECORD_TYPES[dstRecordType]


    # SNMP worker

    def cbFun(snmpEngine, sendRequestHandle, errorIndication,
              errorStatus, errorIndex, varBindTable, cbCtx):

        if errorIndication and not cbCtx['retries']:
            cbCtx['errors'] += 1
            log.error('SNMP Engine error: %s' % errorIndication)
            return

        # SNMPv1 response may contain noSuchName error *and* SNMPv2c exception,
        # so we ignore noSuchName error here
        if errorStatus and errorStatus != 2 or errorIndication:
            log.error(
                'Remote SNMP error %s' % (
                        errorIndication or errorStatus.prettyPrint()))

            if cbCtx['retries']:
                try:
                    nextOID = varBindTable[-1][0][0]

                except IndexError:
                    nextOID = cbCtx['lastOID']

                else:
                    log.error('Failed OID: %s' % nextOID)

                # fuzzy logic of walking a broken OID
                if len(nextOID) < 4:
                    pass

                elif (continueOnErrors - cbCtx['retries']) * 10 / continueOnErrors > 5:
                    nextOID = nextOID[:-2] + (nextOID[-2] + 1,)

                elif nextOID[-1]:
                    nextOID = nextOID[:-1] + (nextOID[-1] + 1,)

                else:
                    nextOID = nextOID[:-2] + (nextOID[-2] + 1, 0)

                cbCtx['retries'] -= 1
                cbCtx['lastOID'] = nextOID

                log.info(
                    'Retrying with OID %s (%s retries left)'
                    '...' % (nextOID, cbCtx['retries']))

                # initiate another SNMP walk iteration
                if getBulkFlag:
                    cmdGen.sendVarBinds(
                        snmpEngine,
                        'tgt',
                        v3ContextEngineId, v3Context,
                        0, getBulkRepetitions,
                        [(nextOID, None)],
                        cbFun, cbCtx)

                else:
                    cmdGen.sendVarBinds(
                        snmpEngine,
                        'tgt',
                        v3ContextEngineId, v3Context,
                        [(nextOID, None)],
                        cbFun, cbCtx)

            cbCtx['errors'] += 1

            return

        if continueOnErrors != cbCtx['retries']:
            cbCtx['retries'] += 1

        if varBindTable and varBindTable[-1] and varBindTable[-1][0]:
            cbCtx['lastOID'] = varBindTable[-1][0][0]

        stopFlag = False

        # Walk var-binds
        for varBindRow in varBindTable:
            for oid, value in varBindRow:

                # EOM
                if stopOID and oid >= stopOID:
                    stopFlag = True  # stop on out of range condition

                elif (value is None or
                          value.tagSet in (rfc1905.NoSuchObject.tagSet,
                                           rfc1905.NoSuchInstance.tagSet,
                                           rfc1905.EndOfMibView.tagSet)):
                    stopFlag = True

                # remove value enumeration
                if value.tagSet == rfc1902.Integer32.tagSet:
                    value = rfc1902.Integer32(value)

                if value.tagSet == rfc1902.Unsigned32.tagSet:
                    value = rfc1902.Unsigned32(value)

                if value.tagSet == rfc1902.Bits.tagSet:
                    value = rfc1902.OctetString(value)

                # Build .snmprec record

                context = {
                    'origOid': oid,
                    'origValue': value,
                    'count': cbCtx['count'],
                    'total': cbCtx['total'],
                    'iteration': cbCtx['iteration'],
                    'reqTime': cbCtx['reqTime'],
                    'startOID': startOID,
                    'stopOID': stopOID,
                    'stopFlag': stopFlag,
                    'variationModule': variationModule
                }

                try:
                    line = dataFileHandler.format(oid, value, **context)

                except error.MoreDataNotification as exc:
                    cbCtx['count'] = 0
                    cbCtx['iteration'] += 1

                    moreDataNotification = exc

                    if 'period' in moreDataNotification:
                        log.info(
                            '%s OIDs dumped, waiting %.2f sec(s)'
                            '...' % (cbCtx['total'],
                                     moreDataNotification['period']))

                        time.sleep(moreDataNotification['period'])

                    # initiate another SNMP walk iteration
                    if getBulkFlag:
                        cmdGen.sendVarBinds(
                            snmpEngine,
                            'tgt',
                            v3ContextEngineId, v3Context,
                            0, getBulkRepetitions,
                            [(startOID, None)],
                            cbFun, cbCtx)

                    else:
                        cmdGen.sendVarBinds(
                            snmpEngine,
                            'tgt',
                            v3ContextEngineId, v3Context,
                            [(startOID, None)],
                            cbFun, cbCtx)

                    stopFlag = True  # stop current iteration

                except error.NoDataNotification:
                    pass

                except error.SnmpsimError as exc:
                    log.error(exc)
                    continue

                else:
                    outputFile.write(line)

                    cbCtx['count'] += 1
                    cbCtx['total'] += 1

                    if cbCtx['count'] % 100 == 0:
                        log.info('OIDs dumped: %s/%s' % (
                            cbCtx['iteration'], cbCtx['count']))

        # Next request time
        cbCtx['reqTime'] = time.time()

        # Continue walking
        return not stopFlag

    cbCtx = {
        'total': 0,
        'count': 0,
        'errors': 0,
        'iteration': 0,
        'reqTime': time.time(),
        'retries': continueOnErrors,
        'lastOID': startOID
    }

    if getBulkFlag:
        cmdGen = cmdgen.BulkCommandGenerator()

        cmdGen.sendVarBinds(
            snmpEngine,
            'tgt',
            v3ContextEngineId, v3Context,
            0, getBulkRepetitions,
            [(startOID, None)],
            cbFun, cbCtx)

    else:
        cmdGen = cmdgen.NextCommandGenerator()

        cmdGen.sendVarBinds(
            snmpEngine,
            'tgt',
            v3ContextEngineId, v3Context,
            [(startOID, None)],
            cbFun, cbCtx)

    log.info(
        'Sending initial %s request for %s (stop at %s)'
        '....' % (getBulkFlag and 'GETBULK' or 'GETNEXT',
                  startOID, stopOID or '<end-of-mib>'))

    started = time.time()

    try:
        snmpEngine.transportDispatcher.runDispatcher()

    except KeyboardInterrupt:
        log.info('Shutting down process...')

    finally:
        if variationModule:
            log.info('Shutting down variation module '
                     '%s...' % variationModuleName)

            try:
                handler = variationModule['shutdown']

                handler(snmpEngine=snmpEngine,
                        options=variationModuleOptions,
                        mode='recording')

            except Exception as exc:
                log.error(
                    'Variation module %s shutdown FAILED: '
                    '%s' % (variationModuleName, exc))

            else:
                log.info(
                    'Variation module %s shutdown OK' % variationModuleName)

        snmpEngine.transportDispatcher.closeDispatcher()

        started = time.time() - started

        cbCtx['total'] += cbCtx['count']

        log.info(
            'OIDs dumped: %s, elapsed: %.2f sec, rate: %.2f OIDs/sec, errors: '
            '%d' % (cbCtx['total'], started,
                    started and cbCtx['count'] // started or 0,
                    cbCtx['errors']))

        outputFile.flush()
        outputFile.close()

        return 0


if __name__ == '__main__':
    try:
        rc = main()

    except KeyboardInterrupt:
        sys.stderr.write('shutting down process...')
        rc = 0

    except Exception as exc:
        sys.stderr.write('process terminated: %s' % exc)

        for line in traceback.format_exception(*sys.exc_info()):
            sys.stderr.write(line.replace('\n', ';'))
        rc = 1

    sys.exit(rc)
