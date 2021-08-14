import uuid
from datetime import datetime
import warnings
import numpy as np
import distutils.version
from pathlib import Path
from typing import Union, Optional, List
from warnings import warn
import psutil
from collections import defaultdict
from copy import deepcopy

import pynwb
from numbers import Real
from hdmf.data_utils import DataChunkIterator
from hdmf.backends.hdf5.h5_utils import H5DataIO
from .json_schema import dict_deep_update
from .basenwbephyswriter import BaseNwbEphysWriter
from .common_writer_tools import ArrayType, PathType, set_dynamic_table_property, check_module, list_get


class BaseSINwbEphysWriter(BaseNwbEphysWriter):
    def __init__(
        self,
        object_to_write,
        nwbfile: pynwb.NWBFile = None,
        metadata: dict = None,
        **kwargs,
    ):
        self.recording, self.sorting, self.waveforms, self.event = None, None, None, None
        BaseNwbEphysWriter.__init__(self, object_to_write, nwbfile=nwbfile, metadata=metadata, **kwargs)

    def add_electrode_groups(self, channel_groups_unique=None):
        channel_groups = self.recording.get_channel_groups()
        if channel_groups is None:
            channel_groups_unique = np.array([0], dtype="int")
        else:
            channel_groups_unique = np.unique(channel_groups)
        super(BaseNwbEphysWriter).add_electrode_groups(channel_groups_unique=channel_groups_unique)