import pynwb
import numpy as np
import uuid
from datetime import datetime
from copy import deepcopy


class BaseNwbEphysWriter:
    def __init__(self, object_to_write, nwb_file_path=None, nwbfile=None, metadata=None, **kwargs):
        self.object_to_write = object_to_write
        assert nwb_file_path is not None or nwbfile is not None, "Use either 'nwbfile' or 'nwb_file_path' arguments!"
        self.nwb_file_path = nwb_file_path
        self.metadata = metadata
        self.nwb_file_path = nwb_file_path
        self.nwbfile = nwbfile
        self._kwargs = kwargs

    def instantiate_nwbfile(self):
        # Default arguments will be over-written if contained in metadata
        nwbfile_kwargs = dict(
            session_description="Auto-generated by NwbRecordingExtractor without description.",
            identifier=str(uuid.uuid4()),
            session_start_time=datetime(1970, 1, 1),
        )
        if self.metadata is not None and "NWBFile" in self.metadata:
            nwbfile_kwargs.update(self.metadata["NWBFile"])
        self.nwbfile = pynwb.NWBFile(**nwbfile_kwargs)

    @staticmethod
    def get_kwargs_description(self):
        raise NotImplementedError

    def write_to_nwb(self):
        raise NotImplementedError

    def write_recording(self):
        raise NotImplementedError

    def write_sorting(self):
        raise NotImplementedError

    def write_epochs(self):
        raise NotImplementedError

    def write_waveforms(self):
        raise NotImplementedError

    def get_nwb_metadata(self):
        raise NotImplementedError

    def add_devices(self, metadata=None):
        """
        Auxiliary static method for nwbextractor.

        Adds device information to nwbfile object.
        Will always ensure nwbfile has at least one device, but multiple
        devices within the metadata list will also be created.

        Missing keys in an element of metadata['Ecephys']['Device'] will be auto-populated with defaults.
        """
        if self.nwbfile is not None:
            assert isinstance(self.nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"

        # Default Device metadata
        defaults = dict(name="Device", description="Ecephys probe. Automatically generated.")

        print("add devices", self.metadata)

        if self.metadata is None:
            self.metadata = dict()

        if metadata is None:
            metadata = deepcopy(self.metadata)

        if "Ecephys" not in metadata:
            metadata["Ecephys"] = dict()

        if "Device" not in metadata["Ecephys"]:
            metadata["Ecephys"]["Device"] = [defaults]

        for dev in metadata["Ecephys"]["Device"]:
            if dev.get("name", defaults["name"]) not in self.nwbfile.devices:
                self.nwbfile.create_device(**dict(defaults, **dev))

        print(self.nwbfile.devices)

    def add_electrodes(self):
        raise NotImplementedError

    def add_electrode_groups(self):
        raise NotImplementedError

    def add_electrical_series(self):
        raise NotImplementedError

    def add_units(self):
        raise NotImplementedError

    def add_epochs(self):
        raise NotImplementedError


def list_get(li: list, idx: int, default):
    """Safe index retrieval from list."""
    try:
        return li[idx]
    except IndexError:
        return default


def set_dynamic_table_property(
    dynamic_table,
    row_ids,
    property_name,
    values,
    index=False,
    default_value=np.nan,
    table=False,
    description="no description",
):
    if not isinstance(row_ids, list) or not all(isinstance(x, int) for x in row_ids):
        raise TypeError("'ids' must be a list of integers")
    ids = list(dynamic_table.id[:])
    if any([i not in ids for i in row_ids]):
        raise ValueError("'ids' contains values outside the range of existing ids")
    if not isinstance(property_name, str):
        raise TypeError("'property_name' must be a string")
    if len(row_ids) != len(values) and index is False:
        raise ValueError("'ids' and 'values' should be lists of same size")

    if index is False:
        if property_name in dynamic_table:
            for (row_id, value) in zip(row_ids, values):
                dynamic_table[property_name].data[ids.index(row_id)] = value
        else:
            col_data = [default_value] * len(ids)  # init with default val
            for (row_id, value) in zip(row_ids, values):
                col_data[ids.index(row_id)] = value
            dynamic_table.add_column(
                name=property_name, description=description, data=col_data, index=index, table=table
            )
    else:
        if property_name in dynamic_table:
            # TODO
            raise NotImplementedError
        else:
            dynamic_table.add_column(name=property_name, description=description, data=values, index=index, table=table)


def check_module(nwbfile, name: str, description: str = None):
    """
    Check if processing module exists. If not, create it. Then return module.

    Parameters
    ----------
    nwbfile: pynwb.NWBFile
    name: str
    description: str | None (optional)

    Returns
    -------
    pynwb.module
    """
    assert isinstance(nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"
    if name in nwbfile.modules:
        return nwbfile.modules[name]
    else:
        if description is None:
            description = name
        return nwbfile.create_processing_module(name, description)
