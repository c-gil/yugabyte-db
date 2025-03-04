#!/usr/bin/env python
#
# Copyright 2021 YugaByte, Inc. and Contributors
#
# Licensed under the Polyform Free Trial License 1.0.0 (the "License"); you
# may not use this file except in compliance with the License. You
# may obtain a copy of the License at
#
# https://github.com/YugaByte/yugabyte-db/blob/master/licenses/POLYFORM-FREE-TRIAL-LICENSE-1.0.0.txt

from __future__ import print_function

import argparse
import atexit
import copy
import logging
import pipes
import random
import shutil
import string
import subprocess
import traceback
import time
import json
import signal
import sys

from argparse import RawDescriptionHelpFormatter
from boto.utils import get_instance_metadata
from datetime import timedelta
from multiprocessing.pool import ThreadPool

import os
import re

TABLET_UUID_LEN = 32
UUID_RE_STR = '[0-9a-f-]{32,36}'
COLOCATED_UUID_SUFFIX = '.colocated.parent.uuid'
COLOCATED_NAME_SUFFIX = '.colocated.parent.tablename'
COLOCATED_UUID_RE_STR = UUID_RE_STR + COLOCATED_UUID_SUFFIX
UUID_ONLY_RE = re.compile('^' + UUID_RE_STR + '$')
NEW_OLD_UUID_RE = re.compile(UUID_RE_STR + '[ ]*\t' + UUID_RE_STR)
COLOCATED_NEW_OLD_UUID_RE = re.compile(COLOCATED_UUID_RE_STR + '[ ]*\t' + COLOCATED_UUID_RE_STR)
LEADING_UUID_RE = re.compile('^(' + UUID_RE_STR + r')\b')
FS_DATA_DIRS_ARG_NAME = '--fs_data_dirs'
FS_DATA_DIRS_ARG_PREFIX = FS_DATA_DIRS_ARG_NAME + '='
RPC_BIND_ADDRESSES_ARG_NAME = '--rpc_bind_addresses'
RPC_BIND_ADDRESSES_ARG_PREFIX = RPC_BIND_ADDRESSES_ARG_NAME + '='

IMPORTED_TABLE_RE = re.compile(r'(?:Colocated t|T)able being imported: ([^\.]*)\.(.*)')
RESTORATION_RE = re.compile('^Restoration id: (' + UUID_RE_STR + r')\b')

STARTED_SNAPSHOT_CREATION_RE = re.compile(r'[\S\s]*Started snapshot creation: (?P<uuid>.*)')
YSQL_CATALOG_VERSION_RE = re.compile(r'[\S\s]*Version: (?P<version>.*)')

PLACEMENT_REGION_RE = re.compile(r'[\S\s]*--placement_region=([\S]*)')

ROCKSDB_PATH_PREFIX = '/yb-data/tserver/data/rocksdb'

SNAPSHOT_DIR_GLOB = '*' + ROCKSDB_PATH_PREFIX + '/table-*/tablet-*.snapshots/*'
SNAPSHOT_DIR_SUFFIX_RE = re.compile(
    '^.*/tablet-({})[.]snapshots/({})$'.format(UUID_RE_STR, UUID_RE_STR))

TABLE_PATH_PREFIX_TEMPLATE = ROCKSDB_PATH_PREFIX + '/table-{}'

TABLET_MASK = 'tablet-????????????????????????????????'
TABLET_DIR_GLOB = '*' + TABLE_PATH_PREFIX_TEMPLATE + '/' + TABLET_MASK

MANIFEST_FILE_NAME = 'Manifest'
METADATA_FILE_NAME = 'SnapshotInfoPB'
SQL_TBSP_DUMP_FILE_NAME = 'YSQLDump_tablespaces'
SQL_DUMP_FILE_NAME = 'YSQLDump'
SQL_DATA_DUMP_FILE_NAME = 'YSQLDump_data'
CREATE_METAFILES_MAX_RETRIES = 10
CLOUD_CFG_FILE_NAME = 'cloud_cfg'
CLOUD_CMD_MAX_RETRIES = 10
RESTORE_DOWNLOAD_LOOP_MAX_RETRIES = 20

CREATE_SNAPSHOT_TIMEOUT_SEC = 60 * 60  # hour
RESTORE_SNAPSHOT_TIMEOUT_SEC = 24 * 60 * 60  # day
SHA_TOOL_PATH = '/usr/bin/sha256sum'
SHA_FILE_EXT = 'sha256'
# Try to read home dir from environment variable, else assume it's /home/yugabyte.
YB_HOME_DIR = os.environ.get("YB_HOME_DIR", "/home/yugabyte")
DEFAULT_REMOTE_YB_ADMIN_PATH = os.path.join(YB_HOME_DIR, 'master/bin/yb-admin')
DEFAULT_REMOTE_YSQL_DUMP_PATH = os.path.join(YB_HOME_DIR, 'master/postgres/bin/ysql_dump')
DEFAULT_REMOTE_YSQL_SHELL_PATH = os.path.join(YB_HOME_DIR, 'master/bin/ysqlsh')
DEFAULT_YB_USER = 'yugabyte'
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PLATFORM_VERSION_FILE_PATH = os.path.join(SCRIPT_DIR, '../../yugaware/conf/version_metadata.json')
YB_VERSION_RE = re.compile(r'^version (\d+\.\d+\.\d+\.\d+).*')

DEFAULT_TS_WEB_PORT = 9000


class BackupException(Exception):
    """A YugaByte backup exception."""
    pass


class CompatibilityException(BackupException):
    """Exception which can be ignored for compatibility."""
    pass


def split_by_tab(line):
    return [item.replace(' ', '') for item in line.split("\t")]


def split_by_space(line):
    items = []
    for item in line.split(" "):
        item = item.strip()
        if item:
            items.append(item)
    return items


def quote_cmd_line_for_bash(cmd_line):
    if not isinstance(cmd_line, list) and not isinstance(cmd_line, tuple):
        raise BackupException("Expected a list/tuple, got: [[ {} ]]".format(cmd_line))
    return ' '.join([pipes.quote(str(arg)) for arg in cmd_line])


class BackupTimer:
    def __init__(self):
        # Store the start time as phase 0.
        self.logged_times = [time.time()]
        self.phases = ["START"]
        self.num_phases = 0

    def log_new_phase(self, msg=""):
        self.logged_times.append(time.time())
        self.phases.append(msg)
        # Print completed time of last stage.
        if self.num_phases > 0:  # Don't print for phase 0 as that is just the start-up.
            time_taken = self.logged_times[self.num_phases] - self.logged_times[self.num_phases - 1]
            logging.info("Completed phase {}: {} [Time taken for phase: {}]".format(
                    self.num_phases,
                    self.phases[self.num_phases],
                    str(timedelta(seconds=time_taken))))
        self.num_phases += 1
        logging.info("[app] Starting phase {}: {}".format(self.num_phases, msg))

    def print_summary(self):
        log_str = "Summary of run:\n"
        # Print info for each phase.
        for i in range(1, self.num_phases + 1):
            t = self.logged_times[i] - self.logged_times[i - 1]
            log_str += "{} : PHASE {} : {}\n".format(str(timedelta(seconds=t)), i, self.phases[i])
        # Also print info for total runtime.
        log_str += "Total runtime: {}".format(
                str(timedelta(seconds=time.time() - self.logged_times[0])))
        # Add [app] for YW platform filter.
        logging.info("[app] " + log_str)

    def start_time_str(self):
        return time_to_str(self.logged_times[0])

    def end_time_str(self):
        return time_to_str(self.logged_times[self.num_phases])


class SingleArgParallelCmd:
    """
    Invokes a single-argument function on the given set of argument values in a parallel way
    using the given thread pool. Arguments are first deduplicated, so they have to be hashable.
    Example:
        SingleArgParallelCmd(fn, [a, b, c]).run(pool)
        -> run in parallel Thread-1: -> fn(a)
                           Thread-2: -> fn(b)
                           Thread-3: -> fn(c)
    """
    def __init__(self, fn, args):
        self.fn = fn
        self.args = args

    def run(self, pool):
        fn_args = sorted(set(self.args))
        return self._run_internal(self.fn, fn_args, fn_args, pool)

    def _run_internal(self, internal_fn, internal_fn_srgs, fn_args, pool):
        values = pool.map(internal_fn, internal_fn_srgs)
        # Return a map from args to the command results.
        assert len(fn_args) == len(values)
        return dict(zip(fn_args, values))


class MultiArgParallelCmd(SingleArgParallelCmd):
    """
    Invokes a function that is allowed to have any number of arguments on the given
    set of tuples of arguments in a parallel way using the given thread pool.
    Arguments are first deduplicated, so they have to be hashable.
    Example:
        MultiArgParallelCmd p(fn)
        p.add_args(a1, a2)
        p.add_args(b1, b2)
        p.run(pool)
        -> run in parallel Thread-1: -> fn(a1, a2)
                           Thread-2: -> fn(b1, b2)
    """
    def __init__(self, fn):
        self.fn = fn
        self.args = []

    def add_args(self, *args_tuple):
        assert isinstance(args_tuple, tuple)
        self.args.append(args_tuple)

    def run(self, pool):
        def internal_fn(args_tuple):
            # One tuple - one function run.
            return self.fn(*args_tuple)

        fn_args = sorted(set(self.args))
        return self._run_internal(internal_fn, fn_args, fn_args, pool)


class SequencedParallelCmd(SingleArgParallelCmd):
    """
    Invokes commands in a parallel way using the given thread pool.
    Each command is a sequence of function calls with the provided arguments.
    Example:
        SequencedParallelCmd p(fn)
        p.start_command()  # Start sequence-1 of the function calls.
        p.add_args(a1, a2)
        p.add_args(b1, b2)
        p.start_command()  # Start sequence-2 of the function calls.
        p.add_args(c1, c2)
        p.add_args(d1, d2)
        p.run(pool)
        -> run in parallel Thread-1: -> fn(a1, a2); fn(b1, b2)
                           Thread-2: -> fn(c1, c2); fn(d1, d2)
    """
    def __init__(self, fn, handle_errors=False):
        self.fn = fn
        self.args = []
        """
        The index is used to return a function call result as the whole command result.
        For example:
            SequencedParallelCmd p(fn)
            p.start_command()
            p.add_args(a1, a2)
            p.add_args(b1, b2)
            p.use_last_fn_result_as_command_result()
            p.add_args(c1, c2)
            p.run(pool)
            -> run -> fn(a1, a2); result = fn(b1, b2); fn(c1, c2); return result
        """
        self.result_fn_call_index = None
        # Whether or not we will throw an error on a cmd failure, or handle it and return a
        # tuple: ('failed-cmd', handle).
        self.handle_errors = handle_errors

    # Handle is returned on failed command if handle_errors=true.
    # Example handle is a tuple of (tablet_id, tserver_ip).
    def start_command(self, handle):
        # Start new set of argument tuples.
        # Place handle at the front.
        self.args.append([handle])

    def use_last_fn_result_as_command_result(self):
        # Let's remember the last fn call index to return its' result as the command result.
        last_fn_call_index = len(self.args[-1]) - 1
        # All commands in the set must have the same index of the result function call.
        assert (self.result_fn_call_index is None or
                self.result_fn_call_index == last_fn_call_index)
        self.result_fn_call_index = last_fn_call_index

    def add_args(self, *args_tuple):
        assert isinstance(args_tuple, tuple)
        assert len(self.args) > 0, 'Call start_command() before'
        self.args[-1].append(args_tuple)

    def run(self, pool):
        def internal_fn(list_of_arg_tuples):
            assert isinstance(list_of_arg_tuples, list)
            # First entry is the handle.
            handle = list_of_arg_tuples[0]
            # Add empty string at beginning to keep len(results) = len(list_of_arg_tuples).
            results = ['']
            # A list of commands: do it one by one.
            for args_tuple in list_of_arg_tuples[1:]:
                assert isinstance(args_tuple, tuple)
                try:
                    results.append(self.fn(*args_tuple))
                except Exception as ex:
                    logging.warning(
                        "Encountered error for handle '{}' while running "
                        "command '{}'. Error: {}".
                        format(handle, args_tuple, ex))
                    if (self.handle_errors):
                        # If we handle errors, then return 'failed-cmd' with the handle.
                        return ('failed-cmd', handle)
                    raise ex

            if self.result_fn_call_index is None:
                return results
            else:
                assert self.result_fn_call_index < len(results)
                return results[self.result_fn_call_index]

        fn_args = [str(list_of_arg_tuples) for list_of_arg_tuples in self.args]
        return self._run_internal(internal_fn, self.args, fn_args, pool)


def check_arg_range(min_value, max_value):
    """
    Return a "checker" function that validates that an argument is within the given range. To be
    used with argparse.
    """
    def check_fn(value):
        value = int(value)
        if value < min_value or value > max_value:
            raise argparse.ArgumentTypeError("Expected a value between {} and {}, got {}".format(
                min_value, max_value, value))
        return value

    return check_fn


def check_uuid(uuid_str):
    """
    A UUID validator for use with argparse.
    """
    if not UUID_ONLY_RE.match(uuid_str):
        raise argparse.ArgumentTypeError("Expected a UUID, got {}".format(uuid_str))
    return uuid_str


def random_string(length):
    return ''.join(random.choice(string.ascii_lowercase) for i in range(length))


def replace_last_substring(s, old, new):
    return new.join(s.rsplit(old, 1)) if s else None


def strip_dir(dir_path):
    return dir_path.rstrip('/\\')


def checksum_path(file_path):
    return file_path + '.' + SHA_FILE_EXT


def checksum_path_downloaded(file_path):
    return checksum_path(file_path) + '.downloaded'


# TODO: get rid of this sed / test program generation in favor of a more maintainable solution.
def key_and_file_filter(checksum_file):
    return "\" $( sed 's| .*/| |' {} ) \"".format(pipes.quote(checksum_file))


# TODO: get rid of this sed / test program generation in favor of a more maintainable solution.
def compare_checksums_cmd(checksum_file1, checksum_file2):
    return "test {} = {} && echo correct || echo invalid".format(
        key_and_file_filter(checksum_file1), key_and_file_filter(checksum_file2))


def get_db_name_cmd(dump_file):
    return "sed -n '/CREATE DATABASE/{s|CREATE DATABASE||;s|WITH.*||;p}' " + pipes.quote(dump_file)


def get_grep_enums_cmd(dump_file):
    return "egrep '^CREATE TYPE ' " + pipes.quote(dump_file) + " || [[ $? == 1 ]]"


def apply_sed_edit_reg_exp_cmd(dump_file, reg_exp):
    return "sed -i '{}' {}".format(reg_exp, pipes.quote(dump_file))


def replace_db_name_cmd(dump_file, old_name, new_name):
    return apply_sed_edit_reg_exp_cmd(
        dump_file, "s|DATABASE {0}|DATABASE {1}|;s|\\\\connect {0}|\\\\connect {1}|".format(
                   old_name, new_name))


def get_table_names_str(keyspaces, tables, delimeter, space):
    if len(keyspaces) != len(tables):
        raise BackupException(
            "Found {} --keyspace keys and {} --table keys. Number of these keys "
            "must be equal.".format(len(keyspaces), len(tables)))

    table_names = []
    for i in range(0, len(tables)):
        table_names.append(delimeter.join([keyspaces[i], tables[i]]))

    return space.join(table_names)


def keyspace_type(keyspace):
    return 'ysql' if ('.' in keyspace) and (keyspace.split('.')[0].lower() == 'ysql') else 'ycql'


def is_parent_colocated_table_name(table_name):
    return table_name.endswith(COLOCATED_NAME_SUFFIX)


def get_postgres_oid_from_table_id(table_id):
    return table_id[-4:]


def verify_colocated_table_ids(old_id, new_id):
    # Assert that the postgres oids are the same.
    if (get_postgres_oid_from_table_id(old_id) != get_postgres_oid_from_table_id(new_id)):
        raise BackupException('Colocated tables have different oids: Old oid: {}, New oid: {}'
                              .format(old_id, new_id))


def keyspace_name(keyspace):
    return keyspace.split('.')[1] if ('.' in keyspace) else keyspace


def time_to_str(time_value):
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(time_value))


def get_yb_backup_version_string():
    str = "unknown"
    try:
        if os.path.isfile(PLATFORM_VERSION_FILE_PATH):
            with open(PLATFORM_VERSION_FILE_PATH, 'r') as f:
                ver_info = json.load(f)

            str = "version {}".format(ver_info['version_number'])
            if 'build_number' in ver_info:
                str += " build {}".format(ver_info['build_number'])
            if 'git_hash' in ver_info:
                str += " revision {}".format(ver_info['git_hash'])
            if 'build_type' in ver_info:
                str += " build_type {}".format(ver_info['build_type'])
            if 'build_timestamp' in ver_info:
                str += " built at {}".format(ver_info['build_timestamp'])
        else:
            logging.warning("[app] Version file not found: {}".format(PLATFORM_VERSION_FILE_PATH))
    except Exception as ex:
        logging.warning("[app] Cannot parse JSON version file {}: {}".
                        format(PLATFORM_VERSION_FILE_PATH, ex))
    finally:
        return str


class BackupOptions:
    def __init__(self, args):
        self.args = args


class AbstractBackupStorage(object):
    def __init__(self, options):
        self.options = options

    @staticmethod
    def storage_type():
        raise BackupException("Unimplemented")

    def _command_list_prefix(self):
        return []


class AzBackupStorage(AbstractBackupStorage):
    def __init__(self, options):
        super(AzBackupStorage, self).__init__(options)

    @staticmethod
    def storage_type():
        return 'az'

    def _command_list_prefix(self):
        return "azcopy"

    def upload_file_cmd(self, src, dest):
        dest = dest + os.getenv('AZURE_STORAGE_SAS_TOKEN')
        return [self._command_list_prefix(), "cp", src, dest]

    def download_file_cmd(self, src, dest):
        src = src + os.getenv('AZURE_STORAGE_SAS_TOKEN')
        return [self._command_list_prefix(), "cp", src,
                dest, "--recursive"]

    def upload_dir_cmd(self, src, dest):
        # azcopy will download the top-level directory as well as the contents without "/*".
        src = "'{}'".format(os.path.join(src, '*'))
        dest = "'{}'".format(dest + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        return ["{} {} {} {} {}".format(self._command_list_prefix(), "cp", src,
                dest, "--recursive")]

    def download_dir_cmd(self, src, dest):
        src = "'{}'".format(os.path.join(src, '*') + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        dest = "'{}'".format(dest)
        return ["{} {} {} {} {}".format(self._command_list_prefix(), "cp", src,
                dest, "--recursive")]

    def delete_obj_cmd(self, dest):
        if dest is None or dest == '/' or dest == '':
            raise BackupException("Destination needs to be well formed.")
        dest = "'{}'".format(dest + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        return ["{} {} {} {}".format(self._command_list_prefix(), "rm", dest, "--recursive=true")]


class GcsBackupStorage(AbstractBackupStorage):
    def __init__(self, options):
        super(GcsBackupStorage, self).__init__(options)

    @staticmethod
    def storage_type():
        return 'gcs'

    def _command_list_prefix(self):
        return ['gsutil', '-o',
                'Credentials:gs_service_key_file=%s' % self.options.cloud_cfg_file_path]

    def upload_file_cmd(self, src, dest):
        return self._command_list_prefix() + ["cp", src, dest]

    def download_file_cmd(self, src, dest):
        return self._command_list_prefix() + ["cp", src, dest]

    def upload_dir_cmd(self, src, dest):
        return self._command_list_prefix() + ["-m", "rsync", "-r", src, dest]

    def download_dir_cmd(self, src, dest):
        return self._command_list_prefix() + ["-m", "rsync", "-r", src, dest]

    def delete_obj_cmd(self, dest):
        if dest is None or dest == '/' or dest == '':
            raise BackupException("Destination needs to be well formed.")
        return self._command_list_prefix() + ["rm", "-r", dest]


class S3BackupStorage(AbstractBackupStorage):
    def __init__(self, options):
        super(S3BackupStorage, self).__init__(options)

    @staticmethod
    def storage_type():
        return 's3'

    def _command_list_prefix(self):
        # If 's3cmd get' fails it creates zero-length file, '--force' is needed to
        # override this empty file on the next retry-step.
        return ['s3cmd', '--force', '--no-check-certificate', '--config=%s'
                % self.options.cloud_cfg_file_path]

    def upload_file_cmd(self, src, dest):
        cmd_list = ["put", src, dest]
        if self.options.args.sse:
            cmd_list.append("--server-side-encryption")
        return self._command_list_prefix() + cmd_list

    def download_file_cmd(self, src, dest):
        return self._command_list_prefix() + ["get", src, dest]

    def upload_dir_cmd(self, src, dest):
        cmd_list = ["sync", "--no-check-md5", src, dest]
        if self.options.args.sse:
            cmd_list.append("--server-side-encryption")
        return self._command_list_prefix() + cmd_list

    def download_dir_cmd(self, src, dest):
        return self._command_list_prefix() + ["sync", "--no-check-md5", src, dest]

    def delete_obj_cmd(self, dest):
        if dest is None or dest == '/' or dest == '':
            raise BackupException("Destination needs to be well formed.")
        return self._command_list_prefix() + ["del", "-r", dest]


class NfsBackupStorage(AbstractBackupStorage):
    def __init__(self, options):
        super(NfsBackupStorage, self).__init__(options)

    @staticmethod
    def storage_type():
        return 'nfs'

    def _command_list_prefix(self):
        result = ['rsync', '-avhW']
        if not self.options.args.mac:
            result.append('--no-compress')
        return result

    # This is a single string because that's what we need for doing `mkdir && rsync`.
    def upload_file_cmd(self, src, dest):
        return ["mkdir -p {} && {} {} {}".format(
            pipes.quote(os.path.dirname(dest)), " ".join(self._command_list_prefix()),
            pipes.quote(src), pipes.quote(dest))]

    def download_file_cmd(self, src, dest):
        return self._command_list_prefix() + [src, dest]

    # This is a list of single string, because a) we need a single string for executing
    # `mkdir && rsync` and b) we need a list of 1 element, as it goes through a tuple().
    def upload_dir_cmd(self, src, dest):
        return ["mkdir -p {} && {} {} {}".format(
            pipes.quote(dest), " ".join(self._command_list_prefix()),
            pipes.quote(src), pipes.quote(dest))]

    def download_dir_cmd(self, src, dest):
        if self.options.args.TEST_sleep_during_download_dir:
            return ["sleep 5 && {} {} {}".format(
                " ".join(self._command_list_prefix()), pipes.quote(src), pipes.quote(dest))]
        return self._command_list_prefix() + [src, dest]

    def delete_obj_cmd(self, dest):
        if dest is None or dest == '/' or dest == '':
            raise BackupException("Destination needs to be well formed.")
        return ["rm", "-rf", pipes.quote(dest)]


BACKUP_STORAGE_ABSTRACTIONS = {
    S3BackupStorage.storage_type(): S3BackupStorage,
    NfsBackupStorage.storage_type(): NfsBackupStorage,
    GcsBackupStorage.storage_type(): GcsBackupStorage,
    AzBackupStorage.storage_type(): AzBackupStorage
}


class KubernetesDetails():
    def __init__(self, server_fqdn, config_map):
        self.namespace = server_fqdn.split('.')[2]
        self.pod_name = server_fqdn.split('.')[0]
        # The pod names are yb-master-n/yb-tserver-n where n is the pod number
        # and yb-master/yb-tserver are the container names.

        # TODO(bhavin192): need to change in case of multiple releases
        # in one namespace. Something like find the word 'master' in
        # the name.

        self.container = self.pod_name.rsplit('-', 1)[0]
        self.env_config = os.environ.copy()
        self.env_config["KUBECONFIG"] = config_map[self.namespace]


def get_instance_profile_credentials():
    result = ()
    iam_credentials_endpoint = 'meta-data/iam/security-credentials/'
    metadata = get_instance_metadata(timeout=1, num_retries=1, data=iam_credentials_endpoint)
    if metadata:
        instance_credentials = next(iter(metadata.values()))
        if isinstance(instance_credentials, dict):
            try:
                access_key = instance_credentials['AccessKeyId']
                secret_key = instance_credentials['SecretAccessKey']
                token = instance_credentials['Token']
                result = access_key, secret_key, token
            except KeyError as e:
                logging.info("Could not find {} in instance metadata".format(e))

    return result


class YBVersion:
    def __init__(self, ver_str, verbose=False):
        self.string = ver_str.strip()
        if verbose:
            logging.info("YB cluster version string: " + self.string)

        matched = YB_VERSION_RE.match(self.string)
        self.parts = matched.group(1).split('.') if matched else None
        if self.parts:
            logging.info("[app] YB cluster version: " + '.'.join(self.parts))


class YBManifest:
    """
    Manifest is the top level JSON-based file-descriptor with the backup content and properties.
    The class is responsible for the data initialization, loading/saving from/into a JSON file.
    The data can be initialized by default/stub values for old backup format (version='0').

    The format can be extended. For example:
      - metadata files can be listed in the locations
      - checksum value can be stored for any tablet-folder
      - any tablet-folder can include complete list of SST files

    Format:
      {
        # This Manifest file format version.
        "version": "1.0",

        # Source cluster parameters.
        "source": {
          # Array of Master IPs.
          "yb-masters": [ "127.0.0.1", ... ],

          # Array of table names in the backup.
          "tables": [ "ysql.yugabyte.tbl1", "ysql.yugabyte.tbl2", ... ],

          # Is it YSQL backup?
          "ysql": true,

          # Is YSQL authentication enabled?
          "ysql_authentication": false,

          # Is Kubernetes used?
          "k8s": false,

          # YB cluster version string. Usually it includes:
          # version_number, build_number, git_hash, build_type, build_timestamp.
          "yb-database-version": "version 2.11.2.0 build PRE_RELEASE ..."
        },

        # Properties of the created backup.
        "properties": {
          # The backup creation start/finish time.
          "start-time": "Mon, 06 Dec 2021 20:45:25 +0000",
          "end-time": "Mon, 06 Dec 2021 20:45:54 +0000",

          # The backup script version string.
          "platform-version": "version 2.11.2.0 build PRE_RELEASE ...",

          # Target storage provider (AWS/GCP/NFS/etc.).
          "storage-type": "nfs",

          # Was additional 'YSQLDump_tablespaces' dump file created?
          "use-tablespaces": true,

          # Parallelism level - number of worker threads.
          "parallelism": 8,

          # True for pg dump based backups.
          "pg_based_backup": false
        },

        # Content of the backup folders/locations.
        "locations": {
          # Single main location.
          <default_backup_location>: {
            # Default location marker.
            "default": true,

            # Tablet folders in the location.
            "tablet-directories": {
              <tablet_id>: {},
              ...
            },
          },

          # Multiple regional locations.
          <regional_location>: {
            # The region name.
            "region": <region_name>,

            # Tablet folders in the location.
            "tablet-directories": {
              <tablet_id>: {},
              ...
            }
          },
          ...
        }
      }
    """
    def __init__(self, backup):
        self.backup = backup
        self.body = {}
        self.body['version'] = "0"

    # Data initialization methods.
    def init(self, snapshot_bucket, pg_based_backup):
        # Call basic initialization by default to prevent code duplication.
        self.create_by_default(self.backup.snapshot_location(snapshot_bucket))
        self.body['version'] = "1.0"

        # Source cluster parameters.
        source = {}
        self.body['source'] = source
        source['ysql'] = self.backup.is_ysql_keyspace()
        source['ysql_authentication'] = self.backup.args.ysql_enable_auth
        source['k8s'] = self.backup.is_k8s()
        source['yb-database-version'] = self.backup.database_version.string
        source['yb-masters'] = self.backup.args.masters.split(",")
        if not pg_based_backup:
            source['tables'] = self.backup.table_names_str().split(" ")

        # The backup properties.
        properties = self.body['properties']
        properties['platform-version'] = get_yb_backup_version_string()
        properties['parallelism'] = self.backup.args.parallelism
        properties['use-tablespaces'] = self.backup.args.use_tablespaces
        properties['pg-based-backup'] = pg_based_backup
        properties['start-time'] = self.backup.timer.start_time_str()
        properties['end-time'] = self.backup.timer.end_time_str()

    def init_locations(self, tablet_leaders, snapshot_bucket):
        locations = self.body['locations']
        for (tablet_id, leader_ip, tserver_region) in tablet_leaders:
            tablet_location = self.backup.snapshot_location(snapshot_bucket, tserver_region)
            if tablet_location not in locations:
                # Init the regional location. Single main location was added in init().
                assert tablet_location != self.backup.args.backup_location
                location_data = {}
                locations[tablet_location] = location_data
                location_data['tablet-directories'] = {}
                location_data['region'] = tserver_region

            locations[tablet_location]['tablet-directories'][tablet_id] = {}

    # Data saving/loading/printing.
    def to_string(self):
        return json.dumps(self.body, indent=2)

    def save_into_file(self, file_path):
        logging.info('[app] Exporting manifest data to {}'.format(file_path))
        with open(file_path, 'w') as f:
            f.write(self.to_string())

    def load_from_file(self, file_path):
        logging.info('[app] Loading manifest data from {}'.format(file_path))
        with open(file_path, 'r') as f:
            self.body = json.load(f)

    def is_loaded(self):
        return self.body['version'] != "0"

    # Stub methods for the old backup format. There is no real Manifest file in the backup.
    def create_by_default(self, default_backup_location):
        properties = {}
        self.body['properties'] = properties
        properties['storage-type'] = self.backup.args.storage_type

        self.body['locations'] = {}
        default_location_data = {}
        # Only one single main location is available in the old backup.
        self.body['locations'][default_backup_location] = default_location_data
        default_location_data['default'] = True
        default_location_data['tablet-directories'] = {}

    # Helper data getters.
    def get_tablet_locations(self, tablet_location):
        assert not tablet_location  # Empty.
        locations = self.body['locations']
        for loc in locations:
            for tablet_id in locations[loc]['tablet-directories']:
                tablet_location[tablet_id] = loc

    def is_pg_based_backup(self):
        pg_based_backup = self.body['properties'].get('pg-based-backup')
        if pg_based_backup is None:
            pg_based_backup = False
        return pg_based_backup


class YBBackup:
    def __init__(self):
        signal.signal(signal.SIGINT, self.cleanup_on_exit)
        signal.signal(signal.SIGTERM, self.cleanup_on_exit)
        self.pools = []
        self.leader_master_ip = ''
        self.ysql_ip = ''
        self.live_tserver_ip = ''
        self.tmp_dir_name = ''
        self.server_ips_with_uploaded_cloud_cfg = {}
        self.k8s_namespace_to_cfg = {}
        self.timer = BackupTimer()
        self.tserver_ip_to_web_port = {}
        self.region_to_location = {}
        self.database_version = YBVersion("unknown")
        self.manifest = YBManifest(self)
        self.parse_arguments()

    def cleanup_on_exit(self, signum, frame):
        for pool in self.pools:
            logging.info("Terminating threadpool ...")
            try:
                pool.terminate()
            except Exception as ex:
                logging.error("Failed to terminate pool: {}".format(ex))
        # Runs clean-up callbacks registered to atexit.
        sys.exit()

    def sleep_or_raise(self, num_retry, timeout, ex):
        if num_retry > 0:
            logging.info("Sleep {}... ({} retries left)".format(timeout, num_retry))
            time.sleep(timeout)
        else:
            raise ex

    def run_program(self, args, num_retry=1, timeout=10, env=None, **kwargs):
        """
        Runs the given program with the given set of arguments. Arguments are the same as in
        subprocess.check_output. Logs the program and the output in verbose mode. Also logs the
        command line in case of failure.
        """
        cmd_as_str = quote_cmd_line_for_bash(args)
        if self.args.verbose:
            logging.info("Running command{}: {}".format(
                "" if num_retry == 1 else " ({} retries)".format(num_retry), cmd_as_str))

        while num_retry > 0:
            num_retry = num_retry - 1

            try:
                proc_env = os.environ.copy()
                proc_env.update(env if env is not None else {})

                subprocess_result = str(subprocess.check_output(
                                         args, stderr=subprocess.STDOUT,
                                         env=proc_env, **kwargs).decode('utf-8', errors='replace'))

                if self.args.verbose:
                    logging.info(
                        "Output from running command [[ {} ]]:\n{}\n[[ END OF OUTPUT ]]".format(
                            cmd_as_str, subprocess_result))
                return subprocess_result
            except subprocess.CalledProcessError as e:
                logging.error("Failed to run command [[ {} ]]: code={} output={}".format(
                    cmd_as_str, e.returncode, str(e.output.decode('utf-8', errors='replace'))))
                self.sleep_or_raise(num_retry, timeout, e)
            except Exception as ex:
                logging.error("Failed to run command [[ {} ]]: {}".format(cmd_as_str, ex))
                self.sleep_or_raise(num_retry, timeout, ex)

    def parse_arguments(self):
        parser = argparse.ArgumentParser(
            description='Backup/restore YB table',
            epilog="Use the following environment variables to provide AWS access and secret "
                   "keys for S3:\n"
                   "    export AWS_ACCESS_KEY_ID=<your_aws_access_key>\n"
                   "    export AWS_SECRET_ACCESS_KEY=<your_aws_secret_key>\n"
                   "For GCS:\n"
                   "    export GCS_CREDENTIALS_JSON=<contents_of_gcp_credentials>\n"
                   "For YCQL tables:\n"
                   "    Keys --keyspace, --table and --table_uuid can be repeated several times.\n"
                   "    Recommended order for creating backup: --keyspace ks1 --table tbl1 "
                   "    --table_uuid uuid1 --keyspace ks2 --table tbl2 --table_uuid uuid2 ...\n"
                   "    Recommended order for restoring backup: --keyspace target_ks --table tbl1 "
                   "    --table tbl2 ...\n"
                   "For YSQL DB:\n"
                   "    Only one key --keyspace is supported. The script processes the whole DB.\n"
                   "    For creating backup: --keyspace ysql.db1\n"
                   "    For restoring backup: --keyspace ysql.db1_copy\n",
            formatter_class=RawDescriptionHelpFormatter)

        parser.add_argument(
            '--masters', required=True,
            help="Comma separated list of masters for the cluster")
        parser.add_argument(
            '--ts_web_hosts_ports', help="Custom TS HTTP hosts and ports. "
                                         "In form: <IP>:<Port>,<IP>:<Port>")
        parser.add_argument(
            '--k8s_config', required=False,
            help="Namespace to use for kubectl in case of kubernetes deployment")
        parser.add_argument(
            '--keyspace', action='append', help="Repeatable keyspace of the tables to backup, "
                                                "or a target keyspace for the backup restoring")
        parser.add_argument(
            '--table', action='append',
            help="Repeatable name of the tables to backup or restore")
        parser.add_argument(
            '--table_uuid', action='append',
            help="Repeatable UUID of the tables to backup.")
        parser.add_argument(
            '--local_yb_admin_binary', help="Path to the local yb-admin binary; "
                                            "by default remote yb-admin tool is used")
        parser.add_argument(
            '--remote_yb_admin_binary', default=DEFAULT_REMOTE_YB_ADMIN_PATH,
            help="Path to the remote yb-admin binary")
        parser.add_argument(
            '--local_ysql_dump_binary', help="Path to the local ysql_dump binary; "
                                             "by default remote ysql_dump tool is used")
        parser.add_argument(
            '--remote_ysql_dump_binary', default=DEFAULT_REMOTE_YSQL_DUMP_PATH,
            help="Path to the remote ysql_dump binary")
        parser.add_argument(
            '--local_ysql_shell_binary', help="Path to the local ysql shell binary; "
                                              "by default remote ysql shell tool is used")
        parser.add_argument(
            '--remote_ysql_shell_binary', default=DEFAULT_REMOTE_YSQL_SHELL_PATH,
            help="Path to the remote ysql shell binary")
        parser.add_argument(
            '--pg_based_backup', action='store_true', default=False, help="Use it to trigger "
                                                                          "pg based backup.")
        parser.add_argument(
            '--detect_enums', action='store_true', default=True, help="Use it to detect enums "
                                                                      "in schema.")
        parser.add_argument(
            '--ssh_key_path', required=False, help="Path to the ssh key file")
        parser.add_argument(
            '--ssh_user', default=DEFAULT_YB_USER, help="Username to use for the ssh connection.")
        parser.add_argument(
            '--remote_user', default=DEFAULT_YB_USER, help="User that will perform backup tasks.")
        parser.add_argument(
            '--ssh_port', default='54422', help="Port to use for the ssh connection.")
        parser.add_argument(
            '--no_ssh', action='store_true', default=False, help="Don't use SSH to run commands")
        parser.add_argument(
            '--mac', action='store_true', default=False, help="Use MacOS tooling")
        parser.add_argument(
            '--ysql_port', help="Custom YSQL process port. "
                                "Default port is used if not specified.")
        parser.add_argument(
            '--ysql_host', help="Custom YSQL process host. "
                                "First alive TS host is used if not specified.")
        parser.add_argument(
            '--ysql_enable_auth', action='store_true', default=False,
            help="Whether ysql authentication is required. If specified, will connect using local "
                 "UNIX socket as the host. Overrides --local_ysql_dump_binary to always "
                 "use remote binary.")
        parser.add_argument(
            '--disable_checksums', action='store_true', default=False,
            help="Whether checksums will be created and checked. If specified, will skip using "
                 "checksums.")

        backup_location_group = parser.add_mutually_exclusive_group(required=True)
        backup_location_group.add_argument(
            '--backup_location',
            help="Directory/bucket under which the snapshots should be created or "
                 "an exact snapshot directory in case of snapshot restoring.")
        # Deprecated flag for backwards compatibility.
        backup_location_group.add_argument('--s3bucket', required=False, help=argparse.SUPPRESS)

        parser.add_argument(
            '--region', action='append',
            help="Repeatable region to create geo-partitioned backup. Every '--region' must have "
                 "related '--region_location' value. For 'restore' it's not used.")
        parser.add_argument(
            '--region_location', action='append',
            help="Repeatable directory/bucket for a region. For 'create' mode it should be "
                 "related to a '--region'. For 'restore' it's not used.")

        parser.add_argument(
            '--no_auto_name',
            action='store_true',
            help="Disable automatic generation of a name under the given backup location. If this "
                 "is specified, the backup location will be the exact path of the directory "
                 "storing the snapshot.")
        parser.add_argument(
            '--no_snapshot_deleting',
            action='store_true',
            help="Disable automatic snapshot deleting after the backup creating or restoring.")
        parser.add_argument(
            '--snapshot_id', type=check_uuid,
            help="Use the existing snapshot ID instead of creating a new one.")
        parser.add_argument(
            '--verbose', required=False, action='store_true', help='Verbose mode')
        parser.add_argument(
            '-j', '--parallelism', type=check_arg_range(1, 100), default=8,
            help='Maximum number of parallel commands to launch. '
                 'This also affects the amount of outgoing s3cmd sync traffic when copying a '
                 'backup to S3.')
        parser.add_argument(
            '--storage_type', choices=list(BACKUP_STORAGE_ABSTRACTIONS.keys()),
            default=S3BackupStorage.storage_type(),
            help="Storage backing for backups, eg: s3, nfs, gcs, ..")
        parser.add_argument(
            'command', choices=['create', 'restore', 'restore_keys', 'delete'],
            help='Create, restore or delete the backup from the provided backup location.')
        parser.add_argument(
            '--certs_dir', required=False,
            help="The directory containing the certs for secure connections.")
        parser.add_argument(
            '--sse', required=False, action='store_true',
            help='Enable server side encryption on storage')
        parser.add_argument(
            '--backup_keys_source', required=False,
            help="Location of universe encryption keys backup file to upload to backup location"
        )
        parser.add_argument(
            '--restore_keys_destination', required=False,
            help="Location to download universe encryption keys backup file to"
        )
        parser.add_argument(
            '--nfs_storage_path', required=False, help="NFS storage mount path")
        parser.add_argument(
            '--restore_time', required=False,
            help='The Unix microsecond timestamp to which to restore the snapshot.')
        parser.add_argument(
            '--use_tablespaces', required=False, action='store_true', default=False,
            help='Backup/restore YSQL TABLESPACE objects into/from the backup.')
        parser.add_argument('--upload', dest='upload', action='store_true', default=True)
        # Please note that we have to use this weird naming (i.e. underscore in the argument name)
        # style to keep it in sync with YB processes G-flags.
        parser.add_argument('--no_upload', dest='upload', action='store_false',
                            help="Skip uploading snapshot")
        parser.add_argument(
            '--edit_ysql_dump_sed_reg_exp', required=False,
            help="Regular expression for 'sed' tool to edit on fly YSQL dump file(s) during the "
                 "backup restoring. Example: \"s|OWNER TO yugabyte|OWNER TO admin|\". WARNING: "
                 "Contact support team before use! No any backward compatibility guaranties.")

        """
        Test arguments
        - Use `argparse.SUPPRESS` to keep these arguments hidden.
        """
        # Adds in a sleep before the rsync command to download data during the restore. Used in
        # tests to hit tablet move race conditions during restores.
        parser.add_argument(
            '--TEST_sleep_during_download_dir', required=False, action='store_true', default=False,
            help=argparse.SUPPRESS)
        self.args = parser.parse_args()

    def post_process_arguments(self):
        if self.args.verbose:
            logging.info("Parsed arguments: {}".format(vars(self.args)))

        if self.args.storage_type == 'nfs':
            logging.info('Checking whether NFS backup storage path mounted on TServers or not')
            pool = ThreadPool(self.args.parallelism)
            self.pools.append(pool)
            tserver_ips = self.get_live_tservers()
            SingleArgParallelCmd(self.find_nfs_storage, tserver_ips).run(pool)

        self.args.backup_location = self.args.backup_location or self.args.s3bucket
        options = BackupOptions(self.args)
        self.cloud_cfg_file_path = os.path.join(self.get_tmp_dir(), CLOUD_CFG_FILE_NAME)
        if self.is_s3():
            if not os.getenv('AWS_SECRET_ACCESS_KEY') and not os.getenv('AWS_ACCESS_KEY_ID'):
                metadata = get_instance_profile_credentials()
                with open(self.cloud_cfg_file_path, 'w') as s3_cfg:
                    if metadata:
                        s3_cfg.write('[default]\n' +
                                     'access_key = ' + metadata[0] + '\n' +
                                     'secret_key = ' + metadata[1] + '\n' +
                                     'access_token = ' + metadata[2] + '\n')
                    else:
                        s3_cfg.write('[default]\n' +
                                     'access_key = ' + '\n' +
                                     'secret_key = ' + '\n' +
                                     'access_token = ' + '\n')
            elif os.getenv('AWS_SECRET_ACCESS_KEY') and os.getenv('AWS_ACCESS_KEY_ID'):
                host_base = os.getenv('AWS_HOST_BASE')
                path_style_access = True if os.getenv('PATH_STYLE_ACCESS',
                                                      "false") == "true" else False
                if host_base:
                    if path_style_access:
                        host_base_cfg = 'host_base = {0}\n' \
                                        'host_bucket = {1}\n'.format(
                                            host_base, self.args.backup_location)
                    else:
                        host_base_cfg = 'host_base = {0}\n' \
                                        'host_bucket = {1}.{0}\n'.format(
                                            host_base, self.args.backup_location)
                else:
                    host_base_cfg = ''
                with open(self.cloud_cfg_file_path, 'w') as s3_cfg:
                    s3_cfg.write('[default]\n' +
                                 'access_key = ' + os.environ['AWS_ACCESS_KEY_ID'] + '\n' +
                                 'secret_key = ' + os.environ['AWS_SECRET_ACCESS_KEY'] + '\n' +
                                 host_base_cfg)
            else:
                raise BackupException(
                    "Missing either AWS access key or secret key for S3 "
                    "in AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY environment variables.")

            os.chmod(self.cloud_cfg_file_path, 0o400)
            options.cloud_cfg_file_path = self.cloud_cfg_file_path
        elif self.is_gcs():
            credentials = os.getenv('GCS_CREDENTIALS_JSON')
            if not credentials:
                raise BackupException(
                    "Set GCP credential file for GCS in GCS_CREDENTIALS_JSON "
                    "environment variable.")
            with open(self.cloud_cfg_file_path, 'w') as cloud_cfg:
                cloud_cfg.write(credentials)
            options.cloud_cfg_file_path = self.cloud_cfg_file_path
        elif self.is_az():
            sas_token = os.getenv('AZURE_STORAGE_SAS_TOKEN')
            if not sas_token:
                raise BackupException(
                    "Set SAS for Azure Storage in AZURE_STORAGE_SAS_TOKEN environment variable.")
            if '?sv' not in sas_token:
                raise BackupException(
                    "SAS tokens must begin with '?sv'.")

        self.storage = BACKUP_STORAGE_ABSTRACTIONS[self.args.storage_type](options)

        if self.is_k8s():
            self.k8s_namespace_to_cfg = json.loads(self.args.k8s_config)
            if self.k8s_namespace_to_cfg is None:
                raise BackupException("Couldn't load k8s configs")

        self.args.local_ysql_dumpall_binary = replace_last_substring(
            self.args.local_ysql_dump_binary, "ysql_dump", "ysql_dumpall")
        self.args.remote_ysql_dumpall_binary = replace_last_substring(
            self.args.remote_ysql_dump_binary, "ysql_dump", "ysql_dumpall")

        if self.args.ts_web_hosts_ports:
            logging.info('TS Web hosts/ports: %s' % (self.args.ts_web_hosts_ports))
            for host_port in self.args.ts_web_hosts_ports.split(','):
                (host, port) = host_port.split(':')
                self.tserver_ip_to_web_port[host] = port

        if self.per_region_backup():
            if len(self.args.region) != len(self.args.region_location):
                raise BackupException(
                    "Found {} --region keys and {} --region_location keys. Number of these keys "
                    "must be equal.".format(len(self.args.region), len(self.args.region_location)))

            for i in range(len(self.args.region)):
                self.region_to_location[self.args.region[i]] = self.args.region_location[i]

    def table_names_str(self, delimeter='.', space=' '):
        return get_table_names_str(self.args.keyspace, self.args.table, delimeter, space)

    def per_region_backup(self):
        return self.args.region_location is not None

    @staticmethod
    def get_snapshot_location(location, bucket):
        if bucket is None:
            return location
        else:
            return os.path.join(location, bucket)

    def snapshot_location(self, bucket, region=None):
        if region is not None:
            if not self.per_region_backup():
                raise BackupException(
                    "Requested region location for non-geo-partitioned backup. Region: {}. "
                    "Locations: {}.".format(region, self.args.region_location))

            if region in self.region_to_location:
                return self.get_snapshot_location(self.region_to_location[region], bucket)

        # Default location.
        return self.get_snapshot_location(self.args.backup_location, bucket)

    def is_s3(self):
        return self.args.storage_type == S3BackupStorage.storage_type()

    def is_gcs(self):
        return self.args.storage_type == GcsBackupStorage.storage_type()

    def is_az(self):
        return self.args.storage_type == AzBackupStorage.storage_type()

    def is_nfs(self):
        return self.args.storage_type == NfsBackupStorage.storage_type()

    def is_k8s(self):
        return self.args.k8s_config is not None

    def is_cloud(self):
        return self.args.storage_type != NfsBackupStorage.storage_type()

    def has_cfg_file(self):
        return self.args.storage_type in [
            GcsBackupStorage.storage_type(), S3BackupStorage.storage_type()]

    def is_ysql_keyspace(self):
        return self.args.keyspace and keyspace_type(self.args.keyspace[0]) == 'ysql'

    def needs_change_user(self):
        return self.args.ssh_user != self.args.remote_user

    def get_main_host_ip(self):
        if self.is_k8s():
            return self.get_live_tserver_ip()
        else:
            return self.get_leader_master_ip()

    def get_leader_master_ip(self):
        if not self.leader_master_ip:
            all_masters = self.args.masters.split(",")
            # Use first Master's ip in list to get list of all masters.
            self.leader_master_ip = all_masters[0].split(':')[0]

            # Get LEADER ip, if it's ALIVE, else any alive master ip.
            output = self.run_yb_admin(['list_all_masters'])
            for line in output.splitlines():
                if LEADING_UUID_RE.match(line):
                    fields = split_by_tab(line)
                    (uuid, ip_port, state, role) = (fields[0], fields[1], fields[2], fields[3])
                    if state == 'ALIVE':
                        (ip, port) = ip_port.split(':')
                        alive_master_ip = ip
                    if role == 'LEADER':
                        break
            self.leader_master_ip = alive_master_ip

        return self.leader_master_ip

    def get_live_tservers(self):
        tserver_ips = []
        output = self.run_yb_admin(['list_all_tablet_servers'])
        for line in output.splitlines():
            if LEADING_UUID_RE.match(line):
                fields = split_by_space(line)
                (ip_port, state) = (fields[1], fields[3])
                if state == 'ALIVE':
                    (ip, port) = ip_port.split(':')
                    tserver_ips.append(ip)
        return tserver_ips

    def get_live_tserver_ip(self):
        if not self.live_tserver_ip:
            alive_ts_ips = self.get_live_tservers()
            if alive_ts_ips:
                all_master_hosts = {hp.split(':')[0] for hp in self.args.masters.split(",")}
                selected_ts_ips = all_master_hosts.intersection(alive_ts_ips)
                # Return a first alive TS if the IP is in the list of Master IPs.
                # Else return just the last alive TS.
                self.live_tserver_ip =\
                    selected_ts_ips.pop() if selected_ts_ips else alive_ts_ips[-1]
            else:
                raise BackupException("Cannot get alive TS: {}".format(alive_ts_ips))

        return self.live_tserver_ip

    def get_ysql_ip(self):
        if not self.ysql_ip:
            output = ""
            if self.args.ysql_enable_auth:
                # Note that this requires YSQL commands to be run on the master leader.
                # In case of k8s, we get live tserver, since master pod does not have
                # pgsql unix socket.
                socket_fds = self.run_ssh_cmd(
                        "ls /tmp/.yb.*/.s.PGSQL.*", self.get_main_host_ip()).strip().split()
                if len(socket_fds):
                    self.ysql_ip = os.path.dirname(socket_fds[0])
                else:
                    output = "Failed to find local socket."
            elif self.args.ysql_host:
                self.ysql_ip = self.args.ysql_host
            else:
                # Get first ALIVE TS.
                self.ysql_ip = self.get_live_tserver_ip()

            if not self.ysql_ip:
                raise BackupException("Cannot get alive TS:\n{}".format(output))

        return self.ysql_ip

    def run_tool(self, local_tool, remote_tool, std_args, cmd_line_args, run_ip=None, env_vars={}):
        """
        Runs the utility from the configured location.
        :param cmd_line_args: command-line arguments to the tool
        :return: the standard output of the tool
        """

        # Use local tool if it's specified.
        if local_tool:
            if not os.path.exists(local_tool):
                raise BackupException("Tool binary not found at {}".format(local_tool))

            return self.run_program([local_tool] + std_args + cmd_line_args,
                                    env=env_vars, num_retry=10)
        else:
            if run_ip:
                run_at_location = run_ip
            else:
                run_at_location = self.get_leader_master_ip()
            # Using remote tool binary on leader master server.
            return self.run_ssh_cmd(
                [remote_tool] + std_args + cmd_line_args,
                run_at_location,
                num_ssh_retry=10,
                env_vars=env_vars)

    def run_yb_admin(self, cmd_line_args, run_ip=None):
        """
        Runs the yb-admin utility from the configured location.
        :param cmd_line_args: command-line arguments to yb-admin
        :return: the standard output of yb-admin
        """

        # Specify cert file in case TLS is enabled.
        cert_flag = []
        if self.args.certs_dir:
            cert_flag = ["--certs_dir_name", self.args.certs_dir]
            cmd_line_args = cert_flag + cmd_line_args

        return self.run_tool(self.args.local_yb_admin_binary, self.args.remote_yb_admin_binary,
                             ['--master_addresses', self.args.masters],
                             cmd_line_args, run_ip=run_ip)

    def get_ysql_dump_std_args(self):
        args = ['--host=' + self.get_ysql_ip()]
        if self.args.ysql_port:
            args += ['--port=' + self.args.ysql_port]
        return args

    def run_cli_tool(self, cli_tool_with_args):
        """
        Runs a command line tool.
        :param cli_tool_with_args: command-line tool with arguments as a single string
        :return: the standard output of the tool
        """

        run_at_ip = self.get_live_tserver_ip() if self.is_k8s() else None
        return self.run_tool(None, cli_tool_with_args, [], [], run_ip=run_at_ip)

    def run_dump_tool(self, local_tool_binary, remote_tool_binary, cmd_line_args):
        """
        Runs the ysql_dump/ysql_dumpall utility from the configured location.
        :param cmd_line_args: command-line arguments to the tool
        :return: the standard output of the tool
        """

        certs_env = {}
        if self.args.certs_dir:
            certs_env = {
                            'FLAGS_certs_dir': self.args.certs_dir,
                            'FLAGS_use_node_to_node_encryption': 'true',
                            'FLAGS_use_node_hostname_for_local_tserver': 'true',
                        }

        run_at_ip = self.get_live_tserver_ip() if self.is_k8s() else None
        # If --ysql_enable_auth is passed, connect with ysql through the remote socket.
        local_binary = None if self.args.ysql_enable_auth else local_tool_binary

        return self.run_tool(local_binary, remote_tool_binary,
                             # Latest tools do not need '--masters', but keep it for backward
                             # compatibility with older YB releases.
                             self.get_ysql_dump_std_args() + ['--masters=' + self.args.masters],
                             cmd_line_args, run_ip=run_at_ip, env_vars=certs_env)

    def run_ysql_dump(self, cmd_line_args):
        return self.run_dump_tool(self.args.local_ysql_dump_binary,
                                  self.args.remote_ysql_dump_binary, cmd_line_args)

    def run_ysql_dumpall(self, cmd_line_args):
        return self.run_dump_tool(self.args.local_ysql_dumpall_binary,
                                  self.args.remote_ysql_dumpall_binary, cmd_line_args)

    def run_ysql_shell(self, cmd_line_args):
        """
        Runs the ysql shell utility from the configured location.
        :param cmd_line_args: command-line arguments to ysql shell
        :return: the standard output of ysql shell
        """
        run_at_ip = None
        if self.is_k8s():
            run_at_ip = self.get_live_tserver_ip()

        return self.run_tool(
            self.args.local_ysql_shell_binary,
            self.args.remote_ysql_shell_binary,
            # Passing dbname template1 explicitly as ysqlsh fails to connect if
            # yugabyte database is deleted. We assume template1 will always be there
            # in ysqlsh.
            self.get_ysql_dump_std_args() + ['--dbname=template1'],
            cmd_line_args,
            run_ip=run_at_ip)

    def create_snapshot(self):
        """
        Creates a new snapshot of the configured table.
        :return: snapshot id
        """
        if self.args.table:
            yb_admin_args = ['create_snapshot'] + self.table_names_str(' ').split(' ')
        elif self.is_ysql_keyspace():
            yb_admin_args = ['create_database_snapshot', self.args.keyspace[0]]
        else:
            yb_admin_args = ['create_keyspace_snapshot', self.args.keyspace[0]]

        output = self.run_yb_admin(yb_admin_args)
        # Ignores any string before and after the creation string + uuid.
        # \S\s matches every character including newlines.
        matched = STARTED_SNAPSHOT_CREATION_RE.match(output)
        if not matched:
            raise BackupException(
                    "Couldn't parse create snapshot output! Expected "
                    "'Started snapshot creation: <id>' in the end: {}".format(output))
        snapshot_id = matched.group('uuid')
        if not UUID_ONLY_RE.match(snapshot_id):
            raise BackupException("Did not get a valid snapshot id out of yb-admin output:\n" +
                                  output)
        return snapshot_id

    def wait_for_snapshot(self, snapshot_id, op, timeout_sec, update_table_list,
                          complete_state='COMPLETE'):
        """
        Waits for the given snapshot to finish being created or restored.
        """
        start_time = time.time()
        snapshot_done = False
        snapshot_tables = []
        snapshot_keyspaces = []
        snapshot_table_uuids = []
        failed_state = 'FAILED'

        yb_admin_args = ['list_snapshots']
        if update_table_list:
            yb_admin_args += ['SHOW_DETAILS']

        while time.time() - start_time < timeout_sec and not snapshot_done:
            output = self.run_yb_admin(yb_admin_args)
            # Expected format:
            # Snapshot UUID                         State
            # 0436035d-c4c5-40c6-b45b-19538849b0d9  COMPLETE
            #   {"type":"NAMESPACE","id":"e4c5591446db417f83a52c679de03118","data":{"name":"a",...}}
            #   {"type":"TABLE","id":"d9603c2cab0b48ec807936496ac0e70e","data":{"name":"t2",...}}
            #   {"type":"TABLE","id":"28b5cebe9b0c4cdaa70ce9ceab31b1e5","data":{\
            #       "name":"t2idx","indexed_table_id":"d9603c2cab0b48ec807936496ac0e70e",...}}
            # c1ad61bf-a42b-4bbb-94f9-28516985c2c5  COMPLETE
            #   ...
            keyspaces = {}
            for line in output.splitlines():
                if not snapshot_done:
                    if line.find(snapshot_id) == 0:
                        snapshot_data = line.split()
                        found_snapshot_id = snapshot_data[0]
                        state = snapshot_data[1]
                        if found_snapshot_id == snapshot_id:
                            if state == complete_state:
                                snapshot_done = True
                                if not update_table_list:
                                    break
                            elif state == failed_state:
                                raise BackupException(
                                    'Snapshot id %s, %s failed!' % (snapshot_id, op))
                elif update_table_list:
                    if line[0] != ' ':
                        break
                    loaded_json = json.loads(line)
                    object_type = loaded_json['type']
                    object_id = loaded_json['id']
                    data = loaded_json['data']
                    if object_type == 'NAMESPACE' and object_id not in keyspaces:
                        keyspace_prefix = 'ysql.' \
                            if data['database_type'] == 'YQL_DATABASE_PGSQL' else ''
                        keyspaces[object_id] = keyspace_prefix + data['name']
                    elif object_type == 'TABLE':
                        snapshot_keyspaces.append(keyspaces[data['namespace_id']])
                        snapshot_tables.append(data['name'])
                        snapshot_table_uuids.append(object_id)

            if not snapshot_done:
                logging.info('Waiting for snapshot %s to complete...' % (op))
                time.sleep(5)

        if not snapshot_done:
            raise BackupException('Timed out waiting for snapshot!')

        if update_table_list:
            if len(snapshot_tables) == 0:
                raise CompatibilityException("Created snapshot does not have tables.")

            if len(snapshot_keyspaces) != len(snapshot_tables):
                raise BackupException(
                    "In the snapshot found {} keyspaces and {} tables. The numbers must be equal.".
                    format(len(snapshot_keyspaces), len(snapshot_tables)))

            self.args.keyspace = snapshot_keyspaces
            self.args.table = snapshot_tables
            self.args.table_uuid = snapshot_table_uuids
            logging.info('Updated list of processing tables: ' + self.table_names_str())

        logging.info('Snapshot id %s %s completed successfully' % (snapshot_id, op))

    def region_for_ts(self, tserver_ip):
        """
        Get region for this TS.
        :param tserver_ip: tablet server ip
        :return: region if found or None
        """
        if not self.per_region_backup():
            return None

        web_port = self.tserver_ip_to_web_port[tserver_ip]\
            if tserver_ip in self.tserver_ip_to_web_port else DEFAULT_TS_WEB_PORT
        url = "{}:{}/varz".format(tserver_ip, web_port)
        output = self.run_program(['curl', url])
        suffix_match = PLACEMENT_REGION_RE.match(output)
        if suffix_match:
            region = suffix_match.group(1)
            logging.info("[app] Region for TS IP {} is '{}'".format(tserver_ip, region))
            return region

        msg = "Cannot get region from {}: [[ {} ]]".format(url, output)
        logging.error("[app] {}".format(msg))
        raise BackupException(msg)

    def find_tablet_leaders(self):
        """
        Lists all tablets and their leaders for the table of interest.
        :return: a list of (tablet id, leader host, leader host region) tuples
        """
        region_by_ts = {}
        tablet_leaders = []

        assert self.args.table
        for i in range(0, len(self.args.table)):
            # Don't call list_tablets on a parent colocated table.
            if is_parent_colocated_table_name(self.args.table[i]):
                continue

            if self.args.table_uuid:
                yb_admin_args = ['list_tablets', 'tableid.' + self.args.table_uuid[i], '0']
            else:
                yb_admin_args = ['list_tablets', self.args.keyspace[i], self.args.table[i], '0']

            output = self.run_yb_admin(yb_admin_args)
            for line in output.splitlines():
                if LEADING_UUID_RE.match(line):
                    fields = split_by_tab(line)
                    (tablet_id, tablet_leader_host_port) = (fields[0], fields[2])
                    (ts_host, ts_port) = tablet_leader_host_port.split(":")

                    if ts_host not in region_by_ts:
                        region = self.region_for_ts(ts_host)
                        region_by_ts[ts_host] = region
                        if region not in self.region_to_location:
                            logging.warning("[app] Cannot find tablet {} location for region {}. "
                                            "Using default location instead.".format(
                                                tablet_id, region))

                    tablet_leaders.append((tablet_id, ts_host, region_by_ts[ts_host]))

        return tablet_leaders

    def create_remote_tmp_dir(self, server_ip):
        if self.args.verbose:
            logging.info("Creating {} on server {}".format(self.get_tmp_dir(), server_ip))

        atexit.register(self.cleanup_remote_temporary_directory, server_ip, self.get_tmp_dir())

        return self.run_ssh_cmd(['mkdir', '-p', self.get_tmp_dir()],
                                server_ip, upload_cloud_cfg=False)

    def upload_local_file_to_server(self, server_ip, local_file_path, dest_dir):
        if self.args.verbose:
            logging.info("Uploading {} to {} on {}".format(local_file_path, dest_dir, server_ip))

        if dest_dir == self.get_tmp_dir():
            output = self.create_remote_tmp_dir(server_ip)
        else:
            if self.args.verbose:
                logging.info("Creating {} on server {}".format(dest_dir, server_ip))
            output = self.run_ssh_cmd(['mkdir', '-p', dest_dir],
                                      server_ip, upload_cloud_cfg=False)

        output += self.upload_file_from_local(server_ip, local_file_path, dest_dir)

        if self.args.verbose:
            logging.info("Uploading {} to {} on {} done: {}".format(
                local_file_path, dest_dir, server_ip, output))

        return output

    def upload_cloud_config(self, server_ip):
        if server_ip not in self.server_ips_with_uploaded_cloud_cfg:
            self.server_ips_with_uploaded_cloud_cfg[server_ip] = self.upload_local_file_to_server(
                server_ip, self.cloud_cfg_file_path, self.get_tmp_dir())

    def upload_file_from_local(self, dest_ip, src, dest):
        output = ''
        if self.is_k8s():
            k8s_details = KubernetesDetails(dest_ip, self.k8s_namespace_to_cfg)
            output += self.run_program([
                'kubectl',
                'cp',
                src,
                '{}/{}:{}'.format(
                    k8s_details.namespace, k8s_details.pod_name, dest),
                '-c',
                k8s_details.container,
                '--no-preserve=true'
            ], env=k8s_details.env_config)
        elif not self.args.no_ssh:
            if self.needs_change_user():
                # TODO: Currently ssh_wrapper_with_sudo.sh will only change users to yugabyte,
                # not args.remote_user.
                ssh_wrapper_path = os.path.join(SCRIPT_DIR, 'ssh_wrapper_with_sudo.sh')
                output += self.run_program(
                    ['scp',
                        '-S', ssh_wrapper_path,
                        '-o', 'StrictHostKeyChecking=no',
                        '-o', 'UserKnownHostsFile=/dev/null',
                        '-i', self.args.ssh_key_path,
                        '-P', self.args.ssh_port,
                        '-q',
                        src,
                        '%s@%s:%s' % (self.args.ssh_user, dest_ip, dest)])
            else:
                output += self.run_program(
                    ['scp',
                        '-o', 'StrictHostKeyChecking=no',
                        '-o', 'UserKnownHostsFile=/dev/null',
                        '-i', self.args.ssh_key_path,
                        '-P', self.args.ssh_port,
                        '-q',
                        src,
                        '%s@%s:%s' % (self.args.ssh_user, dest_ip, dest)])

        return output

    def download_file_to_local(self, src_ip, src, dest):
        output = ''
        if self.is_k8s():
            k8s_details = KubernetesDetails(src_ip, self.k8s_namespace_to_cfg)
            output += self.run_program([
                'kubectl',
                'cp',
                '{}/{}:{}'.format(
                    k8s_details.namespace, k8s_details.pod_name, src),
                dest,
                '-c',
                k8s_details.container,
                '--no-preserve=true'
            ], env=k8s_details.env_config)
        elif not self.args.no_ssh:
            if self.needs_change_user():
                # TODO: Currently ssh_wrapper_with_sudo.sh will only change users to yugabyte,
                # not args.remote_user.
                ssh_wrapper_path = os.path.join(SCRIPT_DIR, 'ssh_wrapper_with_sudo.sh')
                output += self.run_program(
                    ['scp',
                        '-S', ssh_wrapper_path,
                        '-o', 'StrictHostKeyChecking=no',
                        '-o', 'UserKnownHostsFile=/dev/null',
                        '-i', self.args.ssh_key_path,
                        '-P', self.args.ssh_port,
                        '-q',
                        '%s@%s:%s' % (self.args.ssh_user, src_ip, src),
                        dest])
            else:
                output += self.run_program(
                    ['scp',
                        '-o', 'StrictHostKeyChecking=no',
                        '-o', 'UserKnownHostsFile=/dev/null',
                        '-i', self.args.ssh_key_path,
                        '-P', self.args.ssh_port,
                        '-q',
                        '%s@%s:%s' % (self.args.ssh_user, src_ip, src),
                        dest])

        return output

    def run_ssh_cmd(self, cmd, server_ip, upload_cloud_cfg=True, num_ssh_retry=3, env_vars={}):
        """
        Runs the given command on the given remote server over SSH.
        :param cmd: either a string, or a list of arguments. In the latter case, each argument
                    is properly escaped before being passed to ssh.
        :param server_ip: IP address or host name of the server to SSH into.
        :return: the standard output of the SSH command
        """
        if upload_cloud_cfg and self.has_cfg_file():
            self.upload_cloud_config(server_ip)

        if self.args.verbose:
            logging.info("Running command {} on server {}".format(cmd, server_ip))

        if not isinstance(cmd, str):
            if len(cmd) == 1:
                cmd = cmd[0]
            else:
                cmd = quote_cmd_line_for_bash(cmd)

        num_retries = CLOUD_CMD_MAX_RETRIES if self.is_cloud() else num_ssh_retry

        if env_vars:
            # Add env vars to the front of the cmd shell-style like "FOO=bar ls -l"
            bash_env_args = " ".join(["{}={}".format(env_name, pipes.quote(env_val)) for
                                     (env_name, env_val) in env_vars.items()])
            cmd = "{} {}".format(bash_env_args, cmd)

        if self.is_k8s():
            k8s_details = KubernetesDetails(server_ip, self.k8s_namespace_to_cfg)
            return self.run_program([
                'kubectl',
                'exec',
                '-t',
                '-n={}'.format(k8s_details.namespace),
                # For k8s, pick the first qualified name, if given a CNAME.
                k8s_details.pod_name,
                '-c',
                k8s_details.container,
                '--',
                'bash',
                '-c',
                cmd],
                num_retry=num_retries,
                env=k8s_details.env_config)
        elif not self.args.no_ssh:
            change_user_cmd = 'sudo -u %s' % (self.args.remote_user) \
                if self.needs_change_user() else ''
            return self.run_program([
                'ssh',
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null',
                '-i', self.args.ssh_key_path,
                '-p', self.args.ssh_port,
                '-q',
                '%s@%s' % (self.args.ssh_user, server_ip),
                'cd / && %s bash -c ' % (change_user_cmd) + pipes.quote(cmd)],
                num_retry=num_retries)
        else:
            return self.run_program(['bash', '-c', cmd])

    def find_data_dirs(self, tserver_ip):
        """
        Finds the data directories on the given tserver. This queries the /varz endpoint of tserver
        and extracts FS_DATA_DIRS_ARG_NAME flag from response.
        :param tserver_ip: tablet server ip
        :return: a list of top-level YB data directories
        """
        web_port = (self.tserver_ip_to_web_port[tserver_ip]
                    if tserver_ip in self.tserver_ip_to_web_port else DEFAULT_TS_WEB_PORT)
        output = self.run_program(['curl', "{}:{}/varz".format(tserver_ip, web_port)])
        data_dirs = []
        for line in output.split('\n'):
            if line.startswith(FS_DATA_DIRS_ARG_PREFIX):
                for data_dir in line[len(FS_DATA_DIRS_ARG_PREFIX):].split(','):
                    data_dir = data_dir.strip()
                    if data_dir:
                        data_dirs.append(data_dir)
                break

        if not data_dirs:
            raise BackupException(
                ("Did not find any data directories in tserver by querying /varz endpoint"
                 " on tserver '{}:{}'. Was looking for '{}', got this: [[ {} ]]").format(
                     tserver_ip, web_port, FS_DATA_DIRS_ARG_PREFIX, output))
        elif self.args.verbose:
            logging.info("Found data directories on tablet server '{}': {}".format(
                tserver_ip, data_dirs))

        return data_dirs

    def generate_snapshot_dirs(self, data_dir_by_tserver, snapshot_id,
                               tablets_by_tserver_ip, table_ids):
        """
        Generate snapshot directories under the given data directory for the given snapshot id
        on the given tservers.
        :param data_dir_by_tserver: data directory on tservers
        :param snapshot_id: snapshot UUID
        :param tablets_by_tserver_ip: a map from tserver ip address to all tablets of our table
            that it is responsible for.
        :param table_ids: new table UUIDs for all tables
        :return: a three-level map: tablet server ip address to a tablet id to all snapshot
            directories for that tablet id that we found.
        """
        tserver_ip_to_tablet_id_to_snapshot_dirs = {}
        deleted_tablets_by_tserver_ip = {}

        tserver_ip_to_tablet_dirs = {}
        for tserver_ip in tablets_by_tserver_ip:
            tserver_ip_to_tablet_dirs.setdefault(tserver_ip, [])

        for table_id in table_ids:
            for tserver_ip in tablets_by_tserver_ip:
                data_dirs = data_dir_by_tserver[tserver_ip]
                tablet_dirs = tserver_ip_to_tablet_dirs[tserver_ip]

                for data_dir in data_dirs:
                    # Find all tablets for this table on this TS in this data_dir:
                    output = self.run_ssh_cmd(
                      ['find', data_dir,
                       '!', '-readable', '-prune', '-o',
                       '-name', TABLET_MASK,
                       '-and',
                       '-wholename', TABLET_DIR_GLOB.format(table_id),
                       '-print'],
                      tserver_ip)
                    tablet_dirs += [line.strip() for line in output.split("\n") if line.strip()]

                if self.args.verbose:
                    msg = "Found tablet directories for table '{}' on  tablet server '{}': {}"
                    logging.info(msg.format(table_id, tserver_ip, tablet_dirs))

                if not tablet_dirs:
                    logging.warning("No tablet directory found for table '{}' on "
                                    "tablet server '{}'.".format(table_id, tserver_ip))

        for tserver_ip in tablets_by_tserver_ip:
            tablets = tablets_by_tserver_ip[tserver_ip]
            tablet_dirs = tserver_ip_to_tablet_dirs[tserver_ip]
            tablet_id_to_snapshot_dirs = \
                tserver_ip_to_tablet_id_to_snapshot_dirs.setdefault(tserver_ip, {})
            deleted_tablets = deleted_tablets_by_tserver_ip.setdefault(tserver_ip, set())
            tablet_dir_by_id = {}

            for tablet_dir in tablet_dirs:
                tablet_dir_by_id[tablet_dir[-TABLET_UUID_LEN:]] = tablet_dir

            for tablet_id in tablets:
                if tablet_id in tablet_dir_by_id:
                    # Tablet was found in a data dir - use this path.
                    snapshot_dir = tablet_dir_by_id[tablet_id] + '.snapshots/' + snapshot_id
                    tablet_id_to_snapshot_dirs.setdefault(tablet_id, set()).add(snapshot_dir)
                else:
                    # Tablet was not found. That means that the tablet was deleted from this TS.
                    # Let's ignore the tablet and allow retry-loop to find and process new
                    # tablet location on the next downloading round.
                    deleted_tablets.add(tablet_id)
                    if self.args.verbose:
                        logging.info("Tablet '{}' directory was not found on "
                                     "tablet server '{}'.".format(tablet_id, tserver_ip))

            if self.args.verbose:
                logging.info("Downloading list for tablet server '{}': {}".format(
                    tserver_ip, tablet_id_to_snapshot_dirs))

            if deleted_tablets:
                logging.info("No snapshot directories generated on tablet server '{}' "
                             "for tablet ids: '{}'".format(tserver_ip, deleted_tablets))

        return (tserver_ip_to_tablet_id_to_snapshot_dirs, deleted_tablets_by_tserver_ip)

    def find_snapshot_directories(self, data_dir, snapshot_id, tserver_ip):
        """
        Find snapshot directories under the given data directory for the given snapshot id on the
        given tserver.
        :param data_dir: top-level data directory
        :param snapshot_id: snapshot UUID
        :param tserver_ip: tablet server IP or host name
        :return: a list of absolute paths of remote snapshot directories for the given snapshot
        """
        output = self.run_ssh_cmd(
            ['find', data_dir,
             '!', '-readable', '-prune', '-o',
             '-name', snapshot_id, '-and',
             '-wholename', SNAPSHOT_DIR_GLOB,
             '-print'],
            tserver_ip)
        return [line.strip() for line in output.split("\n") if line.strip()]

    def upload_snapshot_directories(self, tablet_leaders, snapshot_id, snapshot_bucket):
        """
        Uploads snapshot directories from all tablet servers hosting our table to subdirectories
        of the given target backup directory.
        :param tablet_leaders: a list of (tablet_id, tserver_ip, tserver_region) tuples
        :param snapshot_id: self-explanatory
        :param snapshot_bucket: the bucket directory under which to upload the data directories
        """
        pool = ThreadPool(self.args.parallelism)
        self.pools.append(pool)

        tablets_by_leader_ip = {}
        location_by_tablet = {}
        for (tablet_id, leader_ip, tserver_region) in tablet_leaders:
            tablets_by_leader_ip.setdefault(leader_ip, set()).add(tablet_id)
            location_by_tablet[tablet_id] = self.snapshot_location(snapshot_bucket, tserver_region)

        tserver_ips = sorted(tablets_by_leader_ip.keys())
        data_dir_by_tserver = SingleArgParallelCmd(self.find_data_dirs, tserver_ips).run(pool)

        for tserver_ip in tserver_ips:
            data_dir_by_tserver[tserver_ip] = copy.deepcopy(data_dir_by_tserver[tserver_ip])

        # Upload config to every TS here to prevent parallel uploading of the config
        # in 'find_snapshot_directories' below.
        if self.has_cfg_file():
            SingleArgParallelCmd(self.upload_cloud_config, tserver_ips).run(pool)

        parallel_find_snapshots = MultiArgParallelCmd(self.find_snapshot_directories)
        tservers_processed = []
        while len(tserver_ips) > len(tservers_processed):
            for tserver_ip in list(tserver_ips):
                if tserver_ip not in tservers_processed:
                    data_dirs = data_dir_by_tserver[tserver_ip]
                    if len(data_dirs) > 0:
                        data_dir = data_dirs[0]
                        parallel_find_snapshots.add_args(data_dir, snapshot_id, tserver_ip)
                        data_dirs.remove(data_dir)

                        if len(data_dirs) == 0:
                            tservers_processed += [tserver_ip]
                    else:
                        tservers_processed += [tserver_ip]

        find_snapshot_dir_results = parallel_find_snapshots.run(pool)

        leader_ip_to_tablet_id_to_snapshot_dirs = self.rearrange_snapshot_dirs(
            find_snapshot_dir_results, snapshot_id, tablets_by_leader_ip)

        parallel_uploads = SequencedParallelCmd(self.run_ssh_cmd)
        self.prepare_cloud_ssh_cmds(
             parallel_uploads, leader_ip_to_tablet_id_to_snapshot_dirs, location_by_tablet,
             snapshot_id, tablets_by_leader_ip, upload=True, snapshot_metadata=None)

        # Run a sequence of steps for each tablet, handling different tablets in parallel.
        parallel_uploads.run(pool)

    def rearrange_snapshot_dirs(
            self, find_snapshot_dir_results, snapshot_id, tablets_by_tserver_ip):
        """
        :param find_snapshot_dir_results: a map from (data_dir, snapshot_id, tserver_ip)
            tuples to the list of snapshot directories under that data directory on that tserver.
            (snapshot_id here is always the single snapshot_id we're dealing with.)
        :param snapshot_id: the snapshot id!
        :param tablets_by_tserver_ip: a map from tserver ip address to all tablets of our table
            that it is responsible for.
        :return: a three-level map: tablet server ip address to a tablet id to all snapshot
            directories for that tablet id that we found.
        """
        tserver_ip_to_tablet_id_to_snapshot_dirs = {}
        for key in find_snapshot_dir_results:
            (data_dir, snapshot_id_unused, tserver_ip) = key
            snapshot_dirs = find_snapshot_dir_results[key]
            assert snapshot_id_unused == snapshot_id
            tablet_id_to_snapshot_dirs =\
                tserver_ip_to_tablet_id_to_snapshot_dirs.setdefault(tserver_ip, {})

            for snapshot_dir in snapshot_dirs:
                suffix_match = SNAPSHOT_DIR_SUFFIX_RE.match(snapshot_dir)
                if not suffix_match:
                    raise BackupException(
                        ("Could not parse tablet id and snapshot id out of snapshot "
                         "directory: '{}'").format(snapshot_dir))
                if snapshot_id != suffix_match.group(2):
                    raise BackupException(
                        "Snapshot directory does not end with snapshot id: '{}'".format(
                            snapshot_dir))

                tablet_id = suffix_match.group(1)
                # During CREATE BACKUP only the LEADER tablet replicas are needed.
                # So, ignore the following warning for FOLLOWERS. It's expected because
                # FOLLOWERS replicas are not in the 'tablets_by_tserver_ip' list
                # (the list 'tablets_by_tserver_ip' contains only the LEADER replicas).
                if tablet_id not in tablets_by_tserver_ip[tserver_ip]:
                    logging.warning(
                        ("Found a snapshot directory '{}' on tablet server '{}' that is not "
                         "present in the list of tablets we are interested in that have this "
                         "tserver hosting it ({}), skipping.").format(
                            snapshot_dir, tserver_ip,
                            ", ".join(sorted(tablets_by_tserver_ip[tserver_ip]))))
                    continue

                tablet_id_to_snapshot_dirs.setdefault(tablet_id, set()).add(snapshot_dir)

        return tserver_ip_to_tablet_id_to_snapshot_dirs

    def create_checksum_cmd_not_quoted(self, file_path, checksum_file_path):
        prefix = pipes.quote(SHA_TOOL_PATH) if not self.args.mac else '/usr/bin/shasum'
        return "{} {} > {}".format(prefix, file_path, checksum_file_path)

    def create_checksum_cmd(self, file_path, checksum_file_path):
        return self.create_checksum_cmd_not_quoted(
            pipes.quote(file_path), pipes.quote(checksum_file_path))

    def create_checksum_cmd_for_dir(self, dir_path):
        return self.create_checksum_cmd_not_quoted(
            os.path.join(pipes.quote(strip_dir(dir_path)), '[!i]*'),
            pipes.quote(checksum_path(strip_dir(dir_path))))

    def prepare_upload_command(self, parallel_commands, snapshot_filepath, tablet_id,
                               tserver_ip, snapshot_dir):
        """
        Prepares the command to upload the backup files to backup location from the tservers.

        :param parallel_commands: result parallel commands to run.
        :param snapshot_filepath: Filepath/cloud url where the backup must be stored.
        :param tablet_id: tablet_id for the tablet whose data we would like to upload.
        :param tserver_ip: tserver ip from which the data needs to be uploaded.
        :param snapshot_dir: The snapshot directory on the tserver from which we need to upload.
        """
        target_tablet_filepath = os.path.join(snapshot_filepath, 'tablet-%s' % (tablet_id))
        if not self.args.disable_checksums:
            logging.info('Creating check-sum for %s on tablet server %s' % (
                         snapshot_dir, tserver_ip))
            create_checksum_cmd = self.create_checksum_cmd_for_dir(snapshot_dir)

            target_checksum_filepath = checksum_path(target_tablet_filepath)
            snapshot_dir_checksum = checksum_path(strip_dir(snapshot_dir))
            logging.info('Uploading %s from tablet server %s to %s URL %s' % (
                         snapshot_dir_checksum, tserver_ip, self.args.storage_type,
                         target_checksum_filepath))
            upload_checksum_cmd = self.storage.upload_file_cmd(
                snapshot_dir_checksum, target_checksum_filepath)

        target_filepath = target_tablet_filepath + '/'
        logging.info('Uploading %s from tablet server %s to %s URL %s' % (
                     snapshot_dir, tserver_ip, self.args.storage_type, target_filepath))
        upload_tablet_cmd = self.storage.upload_dir_cmd(snapshot_dir, target_filepath)

        # Commands to be run on TSes over ssh for uploading the tablet backup.
        if not self.args.disable_checksums:
            # 1. Create check-sum file (via sha256sum tool).
            parallel_commands.add_args(create_checksum_cmd, tserver_ip)
            # 2. Upload check-sum file.
            parallel_commands.add_args(tuple(upload_checksum_cmd), tserver_ip)
        # 3. Upload tablet folder.
        parallel_commands.add_args(tuple(upload_tablet_cmd), tserver_ip)

    def prepare_download_command(self, parallel_commands, tablet_id,
                                 tserver_ip, snapshot_dir, snapshot_metadata):
        """
        Prepares the command to download the backup files to the tservers.

        :param parallel_commands: result parallel commands to run.
        :param tablet_id: tablet_id for the tablet whose data we would like to download.
        :param tserver_ip: tserver ip from which the data needs to be downloaded.
        :param snapshot_dir: The snapshot directory on the tserver to which we need to download.
        """
        if tablet_id not in snapshot_metadata['tablet']:
            raise BackupException('Could not find metadata for tablet id {}'.format(tablet_id))

        old_tablet_id = snapshot_metadata['tablet'][tablet_id]
        snapshot_filepath = snapshot_metadata['tablet_location'][old_tablet_id]
        source_filepath = os.path.join(snapshot_filepath, 'tablet-%s/' % (old_tablet_id))
        snapshot_dir_tmp = strip_dir(snapshot_dir) + '.tmp/'
        logging.info('Downloading %s from %s to %s on tablet server %s' % (source_filepath,
                     self.args.storage_type, snapshot_dir_tmp, tserver_ip))

        # Download the data to a tmp directory and then move it in place.
        cmd = self.storage.download_dir_cmd(source_filepath, snapshot_dir_tmp)

        source_checksum_filepath = checksum_path(
            os.path.join(snapshot_filepath, 'tablet-%s' % (old_tablet_id)))
        snapshot_dir_checksum = checksum_path_downloaded(strip_dir(snapshot_dir))
        cmd_checksum = self.storage.download_file_cmd(
            source_checksum_filepath, snapshot_dir_checksum)

        create_checksum_cmd = self.create_checksum_cmd_for_dir(snapshot_dir_tmp)
        check_checksum_cmd = compare_checksums_cmd(
            snapshot_dir_checksum, checksum_path(strip_dir(snapshot_dir_tmp)))

        rmcmd = ['rm', '-rf', snapshot_dir]
        mkdircmd = ['mkdir', '-p', snapshot_dir_tmp]
        mvcmd = ['mv', snapshot_dir_tmp, snapshot_dir]

        # Commands to be run over ssh for downloading the tablet backup.
        # 1. Clean-up: delete target tablet folder.
        parallel_commands.add_args(tuple(rmcmd), tserver_ip)
        # 2. Create temporary snapshot dir.
        parallel_commands.add_args(tuple(mkdircmd), tserver_ip)
        # 3. Download tablet folder.
        parallel_commands.add_args(tuple(cmd), tserver_ip)
        if not self.args.disable_checksums:
            # 4. Download check-sum file.
            parallel_commands.add_args(tuple(cmd_checksum), tserver_ip)
            # 5. Create new check-sum file.
            parallel_commands.add_args(create_checksum_cmd, tserver_ip)
            # 6. Compare check-sum files.
            parallel_commands.add_args(check_checksum_cmd, tserver_ip)
            parallel_commands.use_last_fn_result_as_command_result()
        # 7. Move the backup in place.
        parallel_commands.add_args(tuple(mvcmd), tserver_ip)

    def prepare_cloud_ssh_cmds(
            self, parallel_commands, tserver_ip_to_tablet_id_to_snapshot_dirs, location_by_tablet,
            snapshot_id, tablets_by_tserver_ip, upload, snapshot_metadata):
        """
        Prepares cloud_command-over-ssh command lines for uploading the snapshot.

        :param parallel_commands: result parallel commands to run.
        :param tserver_ip_to_tablet_id_to_snapshot_dirs: the three-level map as returned by
            rearrange_snapshot_dirs.
        :param location_by_tablet: target cloud URL for every tablet to create snapshot
            directories under
        :param snapshot_id: the snapshot id we're dealing with
        :param tablets_by_tserver_ip: a map from tserver ip to all tablet ids that tserver is the
            responsible for.
        :param upload: True if we are uploading files to cloud, false if we are downloading files
            from cloud.
        :param snapshot_metadata: In case of downloading files from cloud to restore a backup,
            this is the snapshot metadata stored in cloud for the backup.
        """
        tserver_ip_to_tablet_ids_with_data_dirs = {}
        for tserver_ip in tserver_ip_to_tablet_id_to_snapshot_dirs:
            tserver_ip_to_tablet_ids_with_data_dirs.setdefault(tserver_ip, set())

        tservers_processed = []
        while len(tserver_ip_to_tablet_id_to_snapshot_dirs) > len(tservers_processed):
            for tserver_ip in list(tserver_ip_to_tablet_id_to_snapshot_dirs):
                if tserver_ip not in tservers_processed:
                    tablet_id_to_snapshot_dirs =\
                        tserver_ip_to_tablet_id_to_snapshot_dirs[tserver_ip]
                    tablet_ids_with_data_dirs = tserver_ip_to_tablet_ids_with_data_dirs[tserver_ip]
                    if len(tablet_id_to_snapshot_dirs) > 0:
                        tablet_id = list(tablet_id_to_snapshot_dirs)[0]
                        snapshot_dirs = tablet_id_to_snapshot_dirs[tablet_id]

                        if len(snapshot_dirs) > 1:
                            raise BackupException(
                                ('Found multiple snapshot directories on tserver {} for snapshot '
                                 'id {}: {}').format(tserver_ip, snapshot_id, snapshot_dirs))

                        assert len(snapshot_dirs) == 1
                        snapshot_dir = list(snapshot_dirs)[0] + '/'
                        # Pass in the tablet_id and tserver_ip, so if we fail then we know on which
                        # tserver and for what tablet we failed on.
                        parallel_commands.start_command((tablet_id, tserver_ip))

                        if upload:
                            self.prepare_upload_command(
                                parallel_commands, location_by_tablet[tablet_id], tablet_id,
                                tserver_ip, snapshot_dir)
                        else:
                            self.prepare_download_command(
                                parallel_commands, tablet_id,
                                tserver_ip, snapshot_dir, snapshot_metadata)

                        tablet_ids_with_data_dirs.add(tablet_id)
                        tablet_id_to_snapshot_dirs.pop(tablet_id)

                        if len(tablet_id_to_snapshot_dirs) == 0:
                            tservers_processed += [tserver_ip]

                            if tablet_ids_with_data_dirs != tablets_by_tserver_ip[tserver_ip]:
                                for possible_tablet_id in tablets_by_tserver_ip[tserver_ip]:
                                    if possible_tablet_id not in tablet_ids_with_data_dirs:
                                        logging.error(
                                            ("No snapshot directory found for tablet id '{}' on "
                                                "tablet server '{}'.").format(
                                                    possible_tablet_id, tserver_ip))
                                raise BackupException("Did not find snapshot directories for some "
                                                      + "tablets on tablet server " + tserver_ip)
                    else:
                        tservers_processed += [tserver_ip]

    def get_tmp_dir(self):
        if not self.tmp_dir_name:
            tmp_dir = '/tmp/yb_backup_' + random_string(16)
            atexit.register(self.cleanup_temporary_directory, tmp_dir)
            self.run_program(['mkdir', '-p', tmp_dir])
            self.tmp_dir_name = tmp_dir

        return self.tmp_dir_name

    def upload_encryption_key_file(self):
        key_file = os.path.basename(self.args.backup_keys_source)
        key_file_dest = os.path.join("/".join(self.args.backup_location.split("/")[:-1]), key_file)
        if self.is_nfs():
            # Upload keys file from local to NFS mount path on DB node.
            self.upload_local_file_to_server(self.get_main_host_ip(),
                                             self.args.backup_keys_source,
                                             os.path.dirname(key_file_dest))
        else:
            self.run_program(self.storage.upload_file_cmd(self.args.backup_keys_source,
                             key_file_dest))
        self.run_program(["rm", self.args.backup_keys_source])

    def download_file_from_server(self, server_ip, file_path, dest_dir):
        if self.args.verbose:
            logging.info("Downloading {} to local dir {} from {}".format(
                file_path, dest_dir, server_ip))

        output = self.download_file_to_local(server_ip, file_path, dest_dir)

        if self.args.verbose:
            logging.info("Downloading {} to local dir {} from {} done: {}".format(
                file_path, dest_dir, server_ip, output))

    def download_encryption_key_file(self):
        key_file = os.path.basename(self.args.restore_keys_destination)
        key_file_src = os.path.join("/".join(self.args.backup_location.split("/")[:-1]), key_file)
        if self.is_nfs():
            # Download keys file from NFS mount path on DB node to local.
            self.download_file_from_server(self.get_main_host_ip(),
                                           key_file_src,
                                           self.args.restore_keys_destination)
        else:
            self.run_program(
                self.storage.download_file_cmd(key_file_src, self.args.restore_keys_destination)
            )

    def delete_bucket_obj(self):
        del_cmd = self.storage.delete_obj_cmd(self.args.backup_location)
        if self.is_nfs():
            self.run_ssh_cmd(del_cmd, self.get_leader_master_ip())
        else:
            self.run_program(del_cmd)

    def find_nfs_storage(self, tserver_ip):
        """
        Finds the NFS storage path mounted on the given tserver.
        if we don't find storage path mounted on given tserver IP we
        raise exception
        :param tserver_ip: tablet server ip
        """
        try:
            self.run_ssh_cmd(['ls', self.args.nfs_storage_path], tserver_ip)
        except Exception as ex:
            raise BackupException(
                ('Did not find nfs backup storage path: %s mounted on tablet server %s'
                 % (self.args.nfs_storage_path, tserver_ip)))

    def upload_metadata_and_checksum(self, src_path, dest_path):
        """
        Upload metadata file and checksum file to the target backup location.
        :param src_path: local metadata file path
        :param dest_path: destination metadata file path
        """
        src_checksum_path = checksum_path(src_path)
        dest_checksum_path = checksum_path(dest_path)

        if self.args.local_yb_admin_binary:
            if not os.path.exists(src_path):
                raise BackupException(
                    "Could not find metadata file at '{}'".format(src_path))

            if not self.args.disable_checksums:
                logging.info('Creating check-sum for %s' % (src_path))
                self.run_program(
                    self.create_checksum_cmd(src_path, src_checksum_path))

                logging.info('Uploading %s to %s' % (src_checksum_path, dest_checksum_path))
                self.run_program(
                    self.storage.upload_file_cmd(src_checksum_path, dest_checksum_path))

            logging.info('Uploading %s to %s' % (src_path, dest_path))
            self.run_program(
                self.storage.upload_file_cmd(src_path, dest_path))
        else:
            server_ip = self.get_main_host_ip()

            if not self.args.disable_checksums:
                logging.info('Creating check-sum for %s on tablet server %s' % (
                             src_path, server_ip))
                self.run_ssh_cmd(
                    self.create_checksum_cmd(src_path, src_checksum_path),
                    server_ip)

                logging.info('Uploading %s from tablet server %s to %s URL %s' % (
                             src_checksum_path, server_ip,
                             self.args.storage_type, dest_checksum_path))
                self.run_ssh_cmd(
                    self.storage.upload_file_cmd(src_checksum_path, dest_checksum_path),
                    server_ip)

            logging.info('Uploading %s from tablet server %s to %s URL %s' % (
                         src_path, server_ip, self.args.storage_type, dest_path))
            self.run_ssh_cmd(
                self.storage.upload_file_cmd(src_path, dest_path),
                server_ip)

    def get_ysql_catalog_version(self):
        """
        Get current YSQL Catalog version.
        :return: YSQL Catalog version
        """
        output = self.run_yb_admin(['ysql_catalog_version'])
        matched = YSQL_CATALOG_VERSION_RE.match(output)
        if not matched:
            raise BackupException(
                    "Couldn't parse ysql_catalog_version output! Expected "
                    "'Version: <number>' in the end: {}".format(output))
        return matched.group('version')

    def is_enum_present(self, sql_dump_path):
        """
        Detects the presence of enum in schema of a YSQL database.
        """
        resp = self.run_cli_tool(get_grep_enums_cmd(sql_dump_path))
        return ' AS ENUM (' in resp if resp else False

    def create_metadata_files(self):
        """
        :return: snapshot_id and list of sql_dump files
        """
        snapshot_id = None
        dump_files = []
        pg_based_backup = self.args.pg_based_backup
        if self.is_ysql_keyspace():
            sql_tbsp_dump_path = os.path.join(
                self.get_tmp_dir(), SQL_TBSP_DUMP_FILE_NAME) if self.args.use_tablespaces else None
            sql_dump_path = os.path.join(self.get_tmp_dir(), SQL_DUMP_FILE_NAME)
            db_name = keyspace_name(self.args.keyspace[0])
            ysql_dump_args = ['--include-yb-metadata', '--serializable-deferrable', '--create',
                              '--schema-only', '--dbname=' + db_name, '--file=' + sql_dump_path]
            if sql_tbsp_dump_path:
                logging.info("[app] Creating ysql dump for tablespaces to {}".format(
                                sql_tbsp_dump_path))
                self.run_ysql_dumpall(['--tablespaces-only', '--file=' + sql_tbsp_dump_path])
                dump_files.append(sql_tbsp_dump_path)
            else:
                ysql_dump_args.append('--no-tablespaces')

            logging.info("[app] Creating ysql dump for DB '{}' to {}".format(
                            db_name, sql_dump_path))
            self.run_ysql_dump(ysql_dump_args)

            if not pg_based_backup and self.args.detect_enums:
                logging.info('Detecting enum in {}'.format(sql_dump_path))
                pg_based_backup = self.is_enum_present(sql_dump_path)

            dump_files.append(sql_dump_path)
            if pg_based_backup:
                sql_data_dump_path = os.path.join(self.get_tmp_dir(), SQL_DATA_DUMP_FILE_NAME)
                logging.info("[app] Performing ysql_dump based backup!")
                self.run_ysql_dump(
                        ['--include-yb-metadata', '--serializable-deferrable', '--data-only',
                            '--dbname=' + db_name, '--file=' + sql_data_dump_path])
                dump_files.append(sql_data_dump_path)

        if not self.args.snapshot_id and not pg_based_backup:
            snapshot_id = self.create_snapshot()
            logging.info("Snapshot started with id: %s" % snapshot_id)
            # TODO: Remove the following try-catch for compatibility to un-relax the code, after
            #       we ensure nobody uses versions < v2.1.4 (after all move to >= v2.1.8).
            try:
                # With 'update_table_list=True' it runs: 'yb-admin list_snapshots SHOW_DETAILS'
                # to get updated list of backed up namespaces and tables. Note that the last
                # argument 'SHOW_DETAILS' is not supported in old YB versions (< v2.1.4).
                self.wait_for_snapshot(snapshot_id, 'creating', CREATE_SNAPSHOT_TIMEOUT_SEC,
                                       update_table_list=True)
            except CompatibilityException as ex:
                logging.info("Ignoring the exception in the compatibility mode: {}".format(ex))
                # In the compatibility mode repeat the command in old style
                # (without the new command line argument 'SHOW_DETAILS').
                # With 'update_table_list=False' it runs: 'yb-admin list_snapshots'.
                self.wait_for_snapshot(snapshot_id, 'creating', CREATE_SNAPSHOT_TIMEOUT_SEC,
                                       update_table_list=False)

            if not self.args.no_snapshot_deleting:
                logging.info("Snapshot %s will be deleted at exit...", snapshot_id)
                atexit.register(self.delete_created_snapshot, snapshot_id)

        return (snapshot_id, dump_files)

    def create_and_upload_metadata_files(self, snapshot_bucket):
        """
        Generates and uploads metadata files describing the given snapshot to the target
        backup location.
        :param snapshot_bucket: Backup subfolder under which to create a path
        :return: snapshot id
        """
        if self.args.snapshot_id:
            logging.info("Using existing snapshot ID: '{}'".format(self.args.snapshot_id))
            snapshot_id = self.args.snapshot_id

        if self.args.local_yb_admin_binary:
            self.run_program(['mkdir', '-p', self.get_tmp_dir()])
        else:
            self.create_remote_tmp_dir(self.get_main_host_ip())

        is_ysql = self.is_ysql_keyspace()
        if is_ysql:
            start_version = self.get_ysql_catalog_version()

        stored_keyspaces = self.args.keyspace
        stored_tables = self.args.table
        stored_table_uuids = self.args.table_uuid
        num_retry = CREATE_METAFILES_MAX_RETRIES

        while num_retry > 0:
            num_retry = num_retry - 1

            (snapshot_id, dump_files) = self.create_metadata_files()

            if is_ysql:
                final_version = self.get_ysql_catalog_version()
                logging.info('[app] YSQL catalog versions: {} - {}'.format(
                             start_version, final_version))
                if final_version == start_version:
                    break  # Ok. No table schema changes during meta data creating.
                else:
                    # wait_for_snapshot() can update the variables - restore them back.
                    self.args.keyspace = stored_keyspaces
                    self.args.table = stored_tables
                    self.args.table_uuid = stored_table_uuids

                    start_version = final_version
                    logging.info('[app] Retry creating metafiles ({} retries left)'.format(
                                 num_retry))
            else:
                break  # Ok. No need to retry for YCQL.

        if num_retry == 0:
            raise BackupException("Couldn't create metafiles due to catalog changes")

        snapshot_filepath = self.snapshot_location(snapshot_bucket)
        if snapshot_id:
            metadata_path = os.path.join(self.get_tmp_dir(), METADATA_FILE_NAME)
            logging.info('[app] Exporting snapshot {} to {}'.format(snapshot_id, metadata_path))
            self.run_yb_admin(['export_snapshot', snapshot_id, metadata_path],
                              run_ip=self.get_main_host_ip())

            self.upload_metadata_and_checksum(metadata_path,
                                              os.path.join(snapshot_filepath, METADATA_FILE_NAME))
        for file_path in dump_files:
            self.upload_metadata_and_checksum(
                file_path, os.path.join(snapshot_filepath, os.path.basename(file_path)))

        return snapshot_id

    def create_and_upload_manifest(self, tablet_leaders, snapshot_bucket, pg_based_backup):
        """
        Generates and uploads metadata file describing the backup properties.
        :param tablet_leaders: a list of (tablet_id, tserver_ip, tserver_region) tuples
        :param snapshot_bucket: the bucket directory under which to upload the data directories
        """
        self.manifest.init(snapshot_bucket, pg_based_backup)
        if not pg_based_backup:
            self.manifest.init_locations(tablet_leaders, snapshot_bucket)

        # Create Manifest file and upload it to tmp dir on the main host.
        metadata_path = os.path.join(self.get_tmp_dir(), MANIFEST_FILE_NAME)
        self.manifest.save_into_file(metadata_path)
        os.chmod(metadata_path, 0o400)
        if not self.args.local_yb_admin_binary:
            self.upload_local_file_to_server(
                self.get_main_host_ip(), metadata_path, self.get_tmp_dir())

        # Upload Manifest and checksum file from the main host to the target backup path.
        snapshot_filepath = self.snapshot_location(snapshot_bucket)
        target_filepath = os.path.join(snapshot_filepath, MANIFEST_FILE_NAME)
        self.upload_metadata_and_checksum(metadata_path, target_filepath)

    def backup_table(self):
        """
        Creates a backup of the given table by creating a snapshot and uploading it to the provided
        backup location.
        """
        if not self.args.keyspace:
            raise BackupException('Need to specify --keyspace')

        if self.args.table:
            if self.is_ysql_keyspace():
                raise BackupException(
                    "Back up for YSQL is only supported at the database level, "
                    "and not at the table level.")

            logging.info('[app] Backing up tables: {} to {}'.format(
                         self.table_names_str(), self.args.backup_location))
        else:
            if len(self.args.keyspace) != 1:
                raise BackupException(
                    "Only one keyspace supported. Found {} --keyspace keys.".
                    format(len(self.args.keyspace)))

            logging.info('[app] Backing up keyspace: {} to {}'.format(
                         self.args.keyspace[0], self.args.backup_location))

        if self.per_region_backup():
            logging.info('[app] Geo-partitioned backup for regions: {}'.format(self.args.region))

        if self.args.no_auto_name:
            snapshot_bucket = None
        else:
            if self.args.table:
                snapshot_bucket = 'table-{}'.format(self.table_names_str('.', '-'))
            else:
                snapshot_bucket = 'keyspace-{}'.format(self.args.keyspace[0])

            if self.args.table_uuid:
                if len(self.args.table) != len(self.args.table_uuid):
                    raise BackupException(
                        "Found {} --table_uuid keys and {} --table keys. Number of these keys "
                        "must be equal.".format(len(self.args.table_uuid), len(self.args.table)))

                snapshot_bucket = '{}-{}'.format(snapshot_bucket, '-'.join(self.args.table_uuid))

        self.timer.log_new_phase("Create and upload snapshot metadata")
        snapshot_id = self.create_and_upload_metadata_files(snapshot_bucket)

        pg_based_backup = snapshot_id is None
        snapshot_locations = {}
        if self.args.upload:
            if self.args.backup_keys_source:
                self.upload_encryption_key_file()

            snapshot_filepath = self.snapshot_location(snapshot_bucket)
            snapshot_locations["snapshot_url"] = snapshot_filepath

            if pg_based_backup:
                self.create_and_upload_manifest(None, snapshot_bucket, pg_based_backup)
                logging.info("[app] PG based backup successful!")
            else:
                self.timer.log_new_phase("Find tablet leaders")
                tablet_leaders = self.find_tablet_leaders()

                self.timer.log_new_phase("Upload snapshot directories")
                self.upload_snapshot_directories(tablet_leaders, snapshot_id, snapshot_bucket)
                self.create_and_upload_manifest(tablet_leaders, snapshot_bucket, pg_based_backup)
                logging.info("[app] Backed up tables {} to {} successfully!".format(
                    self.table_names_str(), snapshot_filepath))

                if self.per_region_backup():
                    for region in self.args.region:
                        regional_filepath = self.snapshot_location(snapshot_bucket, region)
                        logging.info("[app] Path for region '{}': {}".format(
                            region, regional_filepath))
                        snapshot_locations[region] = regional_filepath

        else:
            snapshot_locations["snapshot_url"] = "UPLOAD_SKIPPED"

        print(json.dumps(snapshot_locations))

    def download_file(self, src_path, target_path):
        """
        Download the file from the external source to the local temporary folder.
        """
        if self.args.local_yb_admin_binary:
            if not self.args.disable_checksums:
                checksum_downloaded = checksum_path_downloaded(target_path)
                self.run_program(
                    self.storage.download_file_cmd(checksum_path(src_path), checksum_downloaded))
            self.run_program(
                self.storage.download_file_cmd(src_path, target_path))

            if not self.args.disable_checksums:
                self.run_program(
                    self.create_checksum_cmd(target_path, checksum_path(target_path)))
                check_checksum_res = self.run_program(
                    compare_checksums_cmd(checksum_downloaded,
                                          checksum_path(target_path))).strip()
        else:
            server_ip = self.get_main_host_ip()

            if not self.args.disable_checksums:
                checksum_downloaded = checksum_path_downloaded(target_path)
                self.run_ssh_cmd(
                    self.storage.download_file_cmd(checksum_path(src_path), checksum_downloaded),
                    server_ip)
            self.run_ssh_cmd(
                self.storage.download_file_cmd(src_path, target_path),
                server_ip)

            if not self.args.disable_checksums:
                self.run_ssh_cmd(
                    self.create_checksum_cmd(target_path, checksum_path(target_path)),
                    server_ip)
                check_checksum_res = self.run_ssh_cmd(
                    compare_checksums_cmd(checksum_downloaded, checksum_path(target_path)),
                    server_ip).strip()

        if (not self.args.disable_checksums) and check_checksum_res != 'correct':
            raise BackupException('Check-sum for {} is {}'.format(
                target_path, check_checksum_res))

        logging.info(
            'Downloaded metadata file %s from %s' % (target_path, src_path))

    def download_metadata_file(self):
        """
        Download the metadata file for a backup so as to perform a restore based on it.
        """
        if self.args.local_yb_admin_binary:
            self.run_program(['mkdir', '-p', self.get_tmp_dir()])
        else:
            self.create_remote_tmp_dir(self.get_main_host_ip())

        dump_files = []
        src_manifest_path = os.path.join(self.args.backup_location, MANIFEST_FILE_NAME)
        manifest_path = os.path.join(self.get_tmp_dir(), MANIFEST_FILE_NAME)
        try:
            self.download_file(src_manifest_path, manifest_path)
            self.download_file_from_server(
                self.get_main_host_ip(), manifest_path, self.get_tmp_dir())
            self.manifest.load_from_file(manifest_path)
        except subprocess.CalledProcessError as ex:
            # The file is available for new backup only.
            if self.args.verbose:
                logging.info("Exception while downloading Manifest file {}. This must be "
                             "a backup from an older version, ignoring: {}".
                             format(src_manifest_path, ex))

        if not self.manifest.is_loaded():
            self.manifest.create_by_default(self.args.backup_location)

        if self.args.verbose:
            logging.info("{} manifest: {}".format(
                "Loaded" if self.manifest.is_loaded() else "Generated",
                self.manifest.to_string()))

        if self.args.use_tablespaces:
            src_sql_tbsp_dump_path = os.path.join(
                self.args.backup_location, SQL_TBSP_DUMP_FILE_NAME)
            sql_tbsp_dump_path = os.path.join(self.get_tmp_dir(), SQL_TBSP_DUMP_FILE_NAME)
            self.download_file(src_sql_tbsp_dump_path, sql_tbsp_dump_path)
            dump_files.append(sql_tbsp_dump_path)
        else:
            sql_tbsp_dump_path = None

        src_sql_dump_path = os.path.join(self.args.backup_location, SQL_DUMP_FILE_NAME)
        sql_dump_path = os.path.join(self.get_tmp_dir(), SQL_DUMP_FILE_NAME)
        try:
            self.download_file(src_sql_dump_path, sql_dump_path)
        except subprocess.CalledProcessError as ex:
            if self.is_ysql_keyspace():
                raise ex
            else:
                # Possibly this is YCQL backup (no way to determite it exactly at this point).
                # Try to ignore YSQL dump - YSQL table restoring will fail a bit later
                # on 'import_snapshot' step.
                logging.info("Ignoring the exception in downloading of {}: {}".
                             format(src_sql_dump_path, ex))
                sql_dump_path = None

        if sql_dump_path:
            dump_files.append(sql_dump_path)
            if self.manifest.is_pg_based_backup():
                src_sql_data_dump_path = os.path.join(
                        self.args.backup_location, SQL_DATA_DUMP_FILE_NAME)
                sql_data_dump_path = os.path.join(self.get_tmp_dir(), SQL_DATA_DUMP_FILE_NAME)
                try:
                    self.download_file(src_sql_data_dump_path, sql_data_dump_path)
                except subprocess.CalledProcessError as ex:
                    raise ex
                dump_files.append(sql_data_dump_path)
                logging.info('Skipping ' + METADATA_FILE_NAME + ' metadata file downloading.')
                return (None, dump_files)

        src_metadata_path = os.path.join(self.args.backup_location, METADATA_FILE_NAME)
        metadata_path = os.path.join(self.get_tmp_dir(), METADATA_FILE_NAME)
        self.download_file(src_metadata_path, metadata_path)

        return (metadata_path, dump_files)

    def import_ysql_dump(self, dump_file_path):
        """
        Import the YSQL dump using the provided file.
        """
        new_db_name = None
        if self.args.keyspace:
            cmd = get_db_name_cmd(dump_file_path)

            if self.args.local_yb_admin_binary:
                old_db_name = self.run_program(cmd).strip()
            else:
                old_db_name = self.run_ssh_cmd(cmd, self.get_main_host_ip()).strip()

            if old_db_name:
                new_db_name = keyspace_name(self.args.keyspace[0])
                if new_db_name == old_db_name:
                    logging.info("[app] Skip renaming because YSQL DB name was not changed: "
                                 "'{}'".format(old_db_name))
                else:
                    logging.info("[app] Renaming YSQL DB from '{}' into '{}'".format(
                                 old_db_name, new_db_name))
                    cmd = replace_db_name_cmd(dump_file_path, old_db_name, new_db_name)

                    if self.args.local_yb_admin_binary:
                        self.run_program(cmd)
                    else:
                        self.run_ssh_cmd(cmd, self.get_main_host_ip())
            else:
                logging.info("[app] Skip renaming because YSQL DB name was not found in file "
                             "{}".format(dump_file_path))

        if self.args.edit_ysql_dump_sed_reg_exp:
            logging.info("[app] Applying sed regular expression '{}' to {}".format(
                         self.args.edit_ysql_dump_sed_reg_exp, dump_file_path))
            cmd = apply_sed_edit_reg_exp_cmd(dump_file_path, self.args.edit_ysql_dump_sed_reg_exp)

            if self.args.local_yb_admin_binary:
                self.run_program(cmd)
            else:
                self.run_ssh_cmd(cmd, self.get_main_host_ip())

        self.run_ysql_shell(['--echo-all', '--file=' + dump_file_path])
        return new_db_name

    def import_snapshot(self, metadata_file_path):
        """
        Import the snapshot metadata using the provided metadata file, process the metadata for
        the imported snapshot and return the snapshot metadata. The snapshot metadata returned is a
        map containing all the metadata for the snapshot and mappings from old ids to new ids for
        table, keyspace, tablets and snapshot.
        """
        yb_admin_args = ['import_snapshot', metadata_file_path]

        if self.args.keyspace:
            yb_admin_args += [self.args.keyspace[0]]

        if self.args.table:
            yb_admin_args += [' '.join(self.args.table)]

        output = self.run_yb_admin(yb_admin_args, run_ip=self.get_main_host_ip())

        snapshot_metadata = {}
        snapshot_metadata['keyspace_name'] = []
        snapshot_metadata['table_name'] = []
        snapshot_metadata['table'] = {}
        snapshot_metadata['tablet'] = {}
        snapshot_metadata['snapshot_id'] = {}
        for idx, line in enumerate(output.splitlines()):
            table_match = IMPORTED_TABLE_RE.search(line)
            if table_match:
                snapshot_metadata['keyspace_name'].append(table_match.group(1))
                snapshot_metadata['table_name'].append(table_match.group(2))
                logging.info('Imported table: {}.{}'.format(table_match.group(1),
                                                            table_match.group(2)))
            elif NEW_OLD_UUID_RE.search(line):
                (entity, old_id, new_id) = split_by_tab(line)
                if entity == 'Table':
                    snapshot_metadata['table'][new_id] = old_id
                    logging.info('Imported table id was changed from {} to {}'.format(old_id,
                                                                                      new_id))
                elif entity.startswith('Tablet'):
                    snapshot_metadata['tablet'][new_id] = old_id
                elif entity == 'Snapshot':
                    snapshot_metadata['snapshot_id']['old'] = old_id
                    snapshot_metadata['snapshot_id']['new'] = new_id
            elif COLOCATED_NEW_OLD_UUID_RE.search(line):
                (entity, old_id, new_id) = split_by_tab(line)
                if entity == 'ParentColocatedTable':
                    verify_colocated_table_ids(old_id, new_id)
                    snapshot_metadata['table'][new_id] = old_id
                    logging.info('Imported colocated table id was changed from {} to {}'
                                 .format(old_id, new_id))
                elif entity == 'ColocatedTable':
                    # A colocated table's tablets are kept under its corresponding parent colocated
                    # table, so we just need to verify the table ids now.
                    verify_colocated_table_ids(old_id, new_id)
                    logging.info('Imported colocated table id was changed from {} to {}'
                                 .format(old_id, new_id))

        tablet_locations = {}
        if self.manifest.is_loaded():
            self.manifest.get_tablet_locations(tablet_locations)
        else:
            default_location = self.args.backup_location
            if self.args.verbose:
                logging.info("Default location for all tablets: {}".format(default_location))

            for tablet_id in snapshot_metadata['tablet'].values():
                tablet_locations[tablet_id] = default_location

        snapshot_metadata['tablet_location'] = tablet_locations
        return snapshot_metadata

    def find_tablet_replicas(self, snapshot_metadata):
        """
        Finds the tablet replicas for tablets present in snapshot_metadata and returns a list of all
        tservers that need to be processed.
        """

        tablets_by_tserver_ip = {}
        for new_id in snapshot_metadata['tablet']:
            output = self.run_yb_admin(['list_tablet_servers', new_id])
            num_ts = 0
            for line in output.splitlines():
                if LEADING_UUID_RE.match(line):
                    fields = split_by_tab(line)
                    (ts_ip_port, role) = (fields[1], fields[2])
                    if role == 'LEADER' or role == 'FOLLOWER':
                        (ts_ip, ts_port) = ts_ip_port.split(':')
                        tablets_by_tserver_ip.setdefault(ts_ip, set()).add(new_id)
                        num_ts += 1
            if num_ts == 0:
                raise BackupException("No alive TS found for tablet {}:\n{}".format(new_id, output))

        return tablets_by_tserver_ip

    def identify_new_tablet_replicas(self, tablets_by_tserver_ip_old, tablets_by_tserver_ip_new):
        """
        Compare old and new sets of tablets per every TServer, find and return difference.
        Returns union of the sets per TServer, and delta of the sets.
        """

        tablets_by_tserver_union = copy.deepcopy(tablets_by_tserver_ip_old)
        tablets_by_tserver_delta = {}

        for ip in tablets_by_tserver_ip_new:
            tablets = tablets_by_tserver_ip_new[ip]
            if ip in tablets_by_tserver_ip_old:
                if not (tablets_by_tserver_ip_old[ip] >= tablets):
                    tablets_by_tserver_union[ip].update(tablets)
                    tablets_by_tserver_delta[ip] = tablets - tablets_by_tserver_ip_old[ip]
            else:
                tablets_by_tserver_union[ip] = tablets
                tablets_by_tserver_delta[ip] = tablets

        return (tablets_by_tserver_union, tablets_by_tserver_delta)

    def download_snapshot_directories(self, snapshot_meta, tablets_by_tserver_to_download,
                                      snapshot_id, table_ids):
        pool = ThreadPool(self.args.parallelism)
        self.pools.append(pool)
        self.timer.log_new_phase("Find all table/tablet data dirs on all tservers")
        tserver_ips = list(tablets_by_tserver_to_download.keys())
        data_dir_by_tserver = SingleArgParallelCmd(self.find_data_dirs, tserver_ips).run(pool)

        if self.args.verbose:
            logging.info('Found data directories: {}'.format(data_dir_by_tserver))

        (tserver_to_tablet_to_snapshot_dirs, tserver_to_deleted_tablets) =\
            self.generate_snapshot_dirs(
                data_dir_by_tserver, snapshot_id, tablets_by_tserver_to_download, table_ids)

        # Remove deleted tablets from the list of planned to be downloaded tablets.
        for tserver_ip in tserver_to_deleted_tablets:
            deleted_tablets = tserver_to_deleted_tablets[tserver_ip]
            tablets_by_tserver_to_download[tserver_ip] -= deleted_tablets

        self.timer.log_new_phase("Download data")
        parallel_downloads = SequencedParallelCmd(self.run_ssh_cmd, handle_errors=True)
        self.prepare_cloud_ssh_cmds(
            parallel_downloads, tserver_to_tablet_to_snapshot_dirs,
            None, snapshot_id, tablets_by_tserver_to_download,
            upload=False, snapshot_metadata=snapshot_meta)

        # Run a sequence of steps for each tablet, handling different tablets in parallel.
        results = parallel_downloads.run(pool)

        for k in results:
            v = results[k]
            if isinstance(v, tuple) and v[0] == 'failed-cmd':
                assert len(v) == 2
                (tablet_id, tserver_ip) = v[1]
                # In case we fail a cmd, don't mark this tablet-tserver pair as succeeded, instead
                # we will retry in the next round of downloads.
                tserver_to_deleted_tablets.setdefault(tserver_ip, set()).add(tablet_id)
            elif not self.args.disable_checksums:
                v = v.strip()
                if v != 'correct':
                    raise BackupException('Check-sum for "{}" is {}'.format(k, v))

        return tserver_to_deleted_tablets

    def restore_table(self):
        """
        Restore a table from the backup stored in the given backup path.
        """
        if self.args.keyspace:
            if len(self.args.keyspace) > 1:
                raise BackupException('Only one --keyspace expected for the restore mode.')
        elif self.args.table:
            raise BackupException('Need to specify --keyspace')

        if self.args.region_location is not None:
            raise BackupException('--region_location is not supported for the restore mode.')

        # TODO (jhe): Perform verification for restore_time. Need to check for:
        #  - Verify that the timestamp given fits in the history retention window for the snapshot
        #  - Verify that we are restoring a keyspace/namespace (no individual tables for pitr)

        logging.info('[app] Restoring backup from {}'.format(self.args.backup_location))
        (metadata_file_path, dump_file_paths) = self.download_metadata_file()
        if len(dump_file_paths):
            self.timer.log_new_phase("Create objects via YSQL dumps")

        for dump_file_path in dump_file_paths:
            dump_file = os.path.basename(dump_file_path)
            if dump_file == SQL_TBSP_DUMP_FILE_NAME:
                logging.info('[app] Create tablespaces from {}'.format(dump_file_path))
                self.import_ysql_dump(dump_file_path)
            elif dump_file == SQL_DUMP_FILE_NAME:
                logging.info('[app] Create YSQL tables from {}'.format(dump_file_path))
                new_db_name = self.import_ysql_dump(dump_file_path)
            elif dump_file == SQL_DATA_DUMP_FILE_NAME:
                # Note: YSQLDump_data must be last in the dump file list.
                self.timer.log_new_phase("Apply complete YSQL data dump")
                ysqlsh_args = ['--file=' + dump_file_path]
                if new_db_name:
                    ysqlsh_args += ['--dbname=' + new_db_name]

                self.run_ysql_shell(ysqlsh_args)

                # Skipping Snapshot loading & restoring because
                # PG based backup means only complete YSQL Data Dump applying.
                logging.info('[app] Restored PG based backup successfully!')
                print(json.dumps({"success": True}))
                return

        self.timer.log_new_phase("Import snapshot")
        snapshot_metadata = self.import_snapshot(metadata_file_path)
        snapshot_id = snapshot_metadata['snapshot_id']['new']
        table_ids = list(snapshot_metadata['table'].keys())

        self.wait_for_snapshot(snapshot_id, 'importing', CREATE_SNAPSHOT_TIMEOUT_SEC, False)

        if not self.args.no_snapshot_deleting:
            logging.info("Snapshot %s will be deleted at exit...", snapshot_id)
            atexit.register(self.delete_created_snapshot, snapshot_id)

        self.timer.log_new_phase("Generate list of tservers for every tablet")
        all_tablets_by_tserver = self.find_tablet_replicas(snapshot_metadata)
        tablets_by_tserver_to_download = all_tablets_by_tserver

        # The loop must stop after a few rounds because the downloading list includes only new
        # tablets for downloading. The downloading list should become smaller with every round
        # and must become empty in the end.
        num_loops = 0
        while tablets_by_tserver_to_download and num_loops < RESTORE_DOWNLOAD_LOOP_MAX_RETRIES:
            num_loops += 1
            logging.info('[app] Downloading tablets onto %d tservers...',
                         len(tablets_by_tserver_to_download))

            if self.args.verbose:
                logging.info('Downloading list: {}'.format(tablets_by_tserver_to_download))

            # Download tablets and get list of deleted tablets.
            tserver_to_deleted_tablets = self.download_snapshot_directories(
                snapshot_metadata, tablets_by_tserver_to_download, snapshot_id, table_ids)

            # Remove deleted tablets from the list of all tablets.
            for tserver_ip in tserver_to_deleted_tablets:
                deleted_tablets = tserver_to_deleted_tablets[tserver_ip]
                all_tablets_by_tserver[tserver_ip] -= deleted_tablets

            self.timer.log_new_phase("Regenerate list of tservers for every tablet")
            tablets_by_tserver_new = self.find_tablet_replicas(snapshot_metadata)
            # Calculate the new downloading list as a subtraction of sets:
            #     downloading_list = NEW_all_tablet_replicas - OLD_all_tablet_replicas
            # And extend the list of all tablets (as unioun of sets) for using it on the next
            # loop iteration:
            #     OLD_all_tablet_replicas = OLD_all_tablet_replicas + NEW_all_tablet_replicas
            #                             = OLD_all_tablet_replicas + downloading_list
            (all_tablets_by_tserver, tablets_by_tserver_to_download) =\
                self.identify_new_tablet_replicas(all_tablets_by_tserver, tablets_by_tserver_new)

        if num_loops >= RESTORE_DOWNLOAD_LOOP_MAX_RETRIES:
            logging.error("Exceeded max number of retries for the restore download loop ({})!".
                          format(RESTORE_DOWNLOAD_LOOP_MAX_RETRIES))

        # Finally, restore the snapshot.
        logging.info('Downloading is finished. Restoring snapshot %s ...', snapshot_id)
        self.timer.log_new_phase("Restore the snapshot")

        restore_snapshot_args = ['restore_snapshot', snapshot_id]
        # Pass in the timestamp if provided.
        if self.args.restore_time:
            restore_snapshot_args.append(self.args.restore_time)

        output = self.run_yb_admin(restore_snapshot_args)

        # Transaction-aware snapshots use special restaration id with final state RESTORED,
        # while previous implementation uses snapshot id and it's state COMPLETE.
        restoration_id = snapshot_id
        complete_restoration_state = 'COMPLETE'
        for line in output.splitlines():
            restoration_match = RESTORATION_RE.match(line)
            if restoration_match:
                restoration_id = restoration_match.group(1)
                complete_restoration_state = 'RESTORED'
                logging.info('[app] Found restoration id: ' + restoration_id)

        self.wait_for_snapshot(restoration_id, 'restoring', RESTORE_SNAPSHOT_TIMEOUT_SEC, False,
                               complete_restoration_state)

        logging.info('[app] Restored backup successfully!')
        print(json.dumps({"success": True}))

    def delete_backup(self):
        """
        Delete the backup specified by the storage location.
        """
        if self.args.backup_location:
            self.delete_bucket_obj()
        logging.info('[app] Deleted backup %s successfully!', self.args.backup_location)
        print(json.dumps({"success": True}))

    def restore_keys(self):
        """
        Restore universe keys from the backup stored in the given backup path.
        """
        if self.args.restore_keys_destination:
            self.download_encryption_key_file()

        logging.info('[app] Restored backup universe keys successfully!')
        print(json.dumps({"success": True}))

    # At exit callbacks
    def cleanup_temporary_directory(self, tmp_dir):
        """
        Callback run on exit to clean up temporary directories.
        """
        if self.args.verbose:
            logging.info("Removing temporary directory '{}'".format(tmp_dir))

        self.run_program(['rm', '-rf', tmp_dir])

    def cleanup_remote_temporary_directory(self, server_ip, tmp_dir):
        """
        Callback run on exit to clean up temporary directories on remote host.
        """
        if self.args.verbose:
            logging.info("Removing remote temporary directory '{}' on {}".format(
                tmp_dir, server_ip))

        self.run_ssh_cmd(['rm', '-rf', tmp_dir], server_ip)

    def delete_created_snapshot(self, snapshot_id):
        """
        Callback run on exit to delete temporary newly created snapshot.
        """
        if self.args.verbose:
            logging.info("Deleting snapshot %s ...", snapshot_id)

        return self.run_yb_admin(['delete_snapshot', snapshot_id])

    def run(self):
        try:
            self.post_process_arguments()
            try:
                self.database_version = YBVersion(self.run_yb_admin(['--version']),
                                                  self.args.verbose)
            except Exception as ex:
                logging.error("Cannot identify YB cluster version. Ignoring the exception: {}".
                              format(ex))

            if self.args.command == 'restore':
                self.restore_table()
            elif self.args.command == 'create':
                self.backup_table()
            elif self.args.command == 'restore_keys':
                self.restore_keys()
            elif self.args.command == 'delete':
                self.delete_backup()
            else:
                logging.error('Command was not specified')
                print(json.dumps({"error": "Command was not specified"}))
        except BackupException as ex:
            print(json.dumps({"error": "Backup exception: {}".format(str(ex))}))
        except Exception as ex:
            print(json.dumps({"error": "Exception: {}".format(str(ex))}))
            traceback.print_exc()
            traceback.print_stack()
        finally:
            self.timer.print_summary()


if __name__ == "__main__":
    # Setup logging. By default in the config the output stream is: stream=sys.stderr.
    # Set custom output format and logging level=INFO (DEBUG messages will not be printed).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    # Registers the signal handlers.
    yb_backup = YBBackup()
    pool = ThreadPool(1)
    try:
        # Main thread cannot be blocked to handle signals.
        future = pool.apply_async(yb_backup.run)
        while not future.ready():
            # Prevent blocking by waiting with timeout.
            future.wait(timeout=1)
    finally:
        logging.info("Terminating parent threadpool ...")
        pool.terminate()
