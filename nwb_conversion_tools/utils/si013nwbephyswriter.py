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

try:
    import spikeextractors as se

    HAVE_SI013 = True
except ImportError:
    HAVE_SI013 = False


_default_sorting_property_descriptions = dict(
    isi_violation="Quality metric that measures the ISI violation ratio as a proxy for the purity of the unit.",
    firing_rate="Number of spikes per unit of time.",
    template="The extracellular average waveform.",
    max_channel="The recording channel id with the largest amplitude.",
    halfwidth="The full-width half maximum of the negative peak computed on the maximum channel.",
    peak_to_valley="The duration between the negative and the positive peaks computed on the maximum channel.",
    snr="The signal-to-noise ratio of the unit.",
    quality="Quality of the unit as defined by phy (good, mua, noise).",
    spike_amplitude="Average amplitude of peaks detected on the channel.",
    spike_rate="Average rate of peaks detected on the channel.",
)


class SI013NwbEphysWriter(BaseNwbEphysWriter):
    """
    Class to write RecordingExtractor and SortingExtractor object from SI<=0.13 to NWB

    Parameters
    ----------
    object_to_write: se.RecordingExtractor or se.SortingExtractor
    nwb_file_path: Path type
    nwbfile: pynwb.NWBFile or None
    metadata: dict or None
    **kwargs: list kwargs and meaning
    """

    def __init__(
        self,
        object_to_write: Union[se.RecordingExtractor, se.SortingExtractor],
        nwb_file_path: PathType = None,
        nwbfile: pynwb.NWBFile = None,
        metadata: dict = None,
        **kwargs,
    ):
        assert HAVE_SI013
        # exclude properties
        if "exclude_properties" in kwargs:
            self._exclude_properties = kwargs["exclude_properties"]
        else:
            self._exclude_properties = []
        if "exclude_features" in kwargs:
            self._exclude_features = kwargs["exclude_features"]
        else:
            self._exclude_features = []

        self.recording, self.sorting = None, None
        BaseNwbEphysWriter.__init__(
            self, object_to_write, nwb_file_path=nwb_file_path, nwbfile=nwbfile, metadata=metadata, **kwargs
        )
        self.recording, self.sorting = None, None
        if isinstance(self.object_to_write, se.RecordingExtractor):
            self.recording = self.object_to_write
        elif isinstance(self.object_to_write, se.SortingExtractor):
            self.sorting = self.object_to_write

    @staticmethod
    def supported_types():
        assert HAVE_SI013
        return (se.RecordingExtractor, se.SortingExtractor)

    def write_to_nwb(self):
        if isinstance(self.object_to_write, se.RecordingExtractor):
            self.write_recording()
        elif isinstance(self.object_to_write, se.SortingExtractor):
            self.write_sorting()

    def write_recording(self):
        if "overwrite" in self._kwargs:
            overwrite = self._kwargs["overwrite"]
        else:
            overwrite = False

        if "write_electrical_series" in self._kwargs:
            write_electrical_series = self._kwargs["write_electrical_series"]
        else:
            write_electrical_series = True

        if self.nwbfile is not None:
            assert isinstance(self.nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"

        assert (
            distutils.version.LooseVersion(pynwb.__version__) >= "1.3.3"
        ), "'write_recording' not supported for version < 1.3.3. Run pip install --upgrade pynwb"

        assert self.nwb_file_path is None or self.nwbfile is None, (
            "Either pass a nwb_file_path location, " "or nwbfile object, but not both!"
        )

        # Update any previous metadata with user passed dictionary
        if hasattr(self.recording, "nwb_metadata"):
            metadata = dict_deep_update(self.recording.nwb_metadata, self.metadata)
        elif self.metadata is None:
            # If not NWBRecording, make metadata from information available on Recording
            self.metadata = self.get_nwb_metadata()

        if self.nwbfile is None:
            if Path(self.nwb_file_path).is_file() and not overwrite:
                read_mode = "r+"
            else:
                read_mode = "w"

            with pynwb.NWBHDF5IO(str(self.nwb_file_path), mode=read_mode) as io:
                if read_mode == "r+":
                    self.nwbfile = io.read()
                else:
                    self.instantiate_nwbfile()

                self.add_devices()
                self.add_electrode_groups()
                self.add_electrodes()
                if write_electrical_series:
                    self.add_electrical_series()
                    self.add_epochs()

                # Write to file
                io.write(self.nwbfile)
        else:
            self.add_devices()
            self.add_electrode_groups()
            self.add_electrodes()
            if write_electrical_series:
                self.add_electrical_series()
                self.add_epochs()

    def write_sorting(self):
        """
        Primary method for writing a SortingExtractor object to an NWBFile.

        Parameters
        ----------
        sorting: SortingExtractor
        save_path: PathType
            Required if an nwbfile is not passed. The location where the NWBFile either exists, or will be written.
        overwrite: bool
            If using save_path, whether or not to overwrite the NWBFile if it already exists.
        nwbfile: NWBFile
            Required if a save_path is not specified. If passed, this function
            will fill the relevant fields within the nwbfile. E.g., calling
            spikeextractors.NwbRecordingExtractor.write_recording(
                my_recording_extractor, my_nwbfile
            )
            will result in the appropriate changes to the my_nwbfile object.
        property_descriptions: dict
            For each key in this dictionary which matches the name of a unit
            property in sorting, adds the value as a description to that
            custom unit column.
        skip_properties: list of str
            Each string in this list that matches a unit property will not be written to the NWBFile.
        skip_features: list of str
            Each string in this list that matches a spike feature will not be written to the NWBFile.
        use_times: bool (optional, defaults to False)
            If True, the times are saved to the nwb file using sorting.frame_to_time(). If False (default),
            the sampling rate is used.
        metadata: dict
            Information for constructing the nwb file (optional).
            Only used if no nwbfile exists at the save_path, and no nwbfile was directly passed.
        """
        if "overwrite" in self._kwargs:
            overwrite = self._kwargs["overwrite"]
        else:
            overwrite = False

        assert self.nwb_file_path is None or self.nwbfile is None, (
            "Either pass a save_path location, " "or nwbfile object, but not both!"
        )
        if self.nwbfile is not None:
            assert isinstance(self.nwbfile, pynwb.NWBFile), "'nwbfile' should be a pynwb.NWBFile object!"

        if self.nwbfile is None:
            if Path(self.nwb_file_path).is_file() and not overwrite:
                read_mode = "r+"
            else:
                read_mode = "w"

            with pynwb.NWBHDF5IO(str(self.nwb_file_path), mode=read_mode) as io:
                if read_mode == "r+":
                    self.nwbfile = io.read()
                else:
                    self.instantiate_nwbfile()
                self.add_units()
                io.write(self.nwbfile)
        else:
            self.add_units()

    def get_nwb_metadata(self):
        """
        Return default metadata for all recording fields.
        """
        metadata = dict(
            NWBFile=dict(
                session_description="Auto-generated by NWB-conversion-tools without description.",
                identifier=str(uuid.uuid4()),
                session_start_time=datetime(1970, 1, 1),
            ),
            Ecephys=dict(
                Device=[dict(name="Device", description="no description")],
                ElectrodeGroup=[
                    dict(name=str(gn), description="no description", location="unknown", device="Device")
                    for gn in np.unique(self.recording.get_channel_groups())
                ],
            ),
        )
        return metadata

    def add_electrodes(self):
        """
        Auxiliary static method for nwbextractor.

        Adds channels from recording object as electrodes to nwbfile object.

        Missing keys in an element of metadata['Ecephys']['ElectrodeGroup'] will be auto-populated with defaults
        whenever possible.

        If 'my_name' is set to one of the required fields for nwbfile
        electrodes (id, x, y, z, imp, loccation, filtering, group_name),
        then the metadata will override their default values.

        Setting 'my_name' to metadata field 'group' is not supported as the linking to
        nwbfile.electrode_groups is handled automatically; please specify the string 'group_name' in this case.

        If no group information is passed via metadata, automatic linking to existing electrode groups,
        possibly including the default, will occur.
        """
        if self.nwbfile.electrodes is not None:
            ids_absent = [id not in self.nwbfile.electrodes.id for id in self.recording.get_channel_ids()]
            if not all(ids_absent):
                warnings.warn("cannot create electrodes for this recording as ids already exist")
                return

        if self.nwbfile is not None:
            assert isinstance(self.nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"
        if self.nwbfile.electrode_groups is None or len(self.nwbfile.electrode_groups) == 0:
            self.add_electrode_groups()
        # For older versions of pynwb, we need to manually add these columns
        if distutils.version.LooseVersion(pynwb.__version__) < "1.3.0":
            if self.nwbfile.electrodes is None or "rel_x" not in self.nwbfile.electrodes.colnames:
                self.nwbfile.add_electrode_column("rel_x", "x position of electrode in electrode group")
            if self.nwbfile.electrodes is None or "rel_y" not in self.nwbfile.electrodes.colnames:
                self.nwbfile.add_electrode_column("rel_y", "y position of electrode in electrode group")

        defaults = dict(
            x=np.nan,
            y=np.nan,
            z=np.nan,
            # There doesn't seem to be a canonical default for impedence, if missing.
            # The NwbRecordingExtractor follows the -1.0 convention, other scripts sometimes use np.nan
            imp=-1.0,
            location="unknown",
            filtering="none",
            group_name="0",
        )
        if self.metadata is None:
            self.metadata = dict(Ecephys=dict())

        if "Ecephys" not in self.metadata:
            self.metadata["Ecephys"] = dict()

        if "Electrodes" not in self.metadata["Ecephys"]:
            self.metadata["Ecephys"]["Electrodes"] = []

        assert all(
            [
                isinstance(x, dict) and set(x.keys()) == set(["name", "description"])
                for x in self.metadata["Ecephys"]["Electrodes"]
            ]
        ), (
            "Expected metadata['Ecephys']['Electrodes'] to be a list of dictionaries, "
            "containing the keys 'name' and 'description'"
        )
        assert all(
            [x["name"] != "group" for x in self.metadata["Ecephys"]["Electrodes"]]
        ), "Passing metadata field 'group' is deprecated; pass group_name instead!"

        if self.nwbfile.electrodes is None:
            nwb_elec_ids = []
        else:
            nwb_elec_ids = self.nwbfile.electrodes.id.data[:]

        elec_columns = defaultdict(dict)  # dict(name: dict(description='',data=data, index=False))
        elec_columns_append = defaultdict(dict)
        property_names = set()
        for chan_id in self.recording.get_channel_ids():
            for i in self.recording.get_channel_property_names(channel_id=chan_id):
                property_names.add(i)

        # property 'brain_area' of RX channels corresponds to 'location' of NWB electrodes
        exclude_names = set(["location", "group"] + list(self._exclude_properties))

        channel_property_defaults = {list: [], np.ndarray: np.array(np.nan), str: "", Real: np.nan}
        found_property_types = {prop: Real for prop in property_names}

        for prop in property_names:
            prop_skip = False
            if prop not in exclude_names:
                data = []
                prop_chan_count = 0
                # build data:
                for chan_id in self.recording.get_channel_ids():
                    if prop in self.recording.get_channel_property_names(channel_id=chan_id):
                        prop_chan_count += 1
                        chan_data = self.recording.get_channel_property(channel_id=chan_id, property_name=prop)
                        # find the type and store (only when the first channel with given property is found):
                        if prop_chan_count == 1:
                            proptype = [
                                proptype for proptype in channel_property_defaults if isinstance(chan_data, proptype)
                            ]
                            if len(proptype) > 0:
                                found_property_types[prop] = proptype[0]
                                # cast as float if any number:
                                if found_property_types[prop] == Real:
                                    chan_data = np.float(chan_data)
                                # update data if wrong datatype items filled prior:
                                if len(data) > 0 and not isinstance(data[-1], found_property_types[prop]):
                                    data = [channel_property_defaults[found_property_types[prop]]] * len(data)
                            else:
                                prop_skip = True  # skip storing that property if not of default type
                                break
                        data.append(chan_data)
                    else:
                        data.append(channel_property_defaults[found_property_types[prop]])
                # store data after build:
                if not prop_skip:
                    index = found_property_types[prop] == ArrayType
                    prop_name_new = "location" if prop == "brain_area" else prop
                    found_property_types[prop_name_new] = found_property_types.pop(prop)
                    elec_columns[prop_name_new].update(description=prop_name_new, data=data, index=index)

        for x in self.metadata["Ecephys"]["Electrodes"]:
            elec_columns[x["name"]]["description"] = x["description"]
            if x["name"] not in list(elec_columns):
                raise ValueError(f'"{x["name"]}" not a property of se object')

        # updating default arguments if electrodes table already present:
        default_updated = dict()
        if self.nwbfile.electrodes is not None:
            for colname in self.nwbfile.electrodes.colnames:
                if colname != "group":
                    samp_data = self.nwbfile.electrodes[colname].data[0]
                    default_datatype = [
                        proptype for proptype in channel_property_defaults if isinstance(samp_data, proptype)
                    ][0]
                    default_updated.update({colname: channel_property_defaults[default_datatype]})
        default_updated.update(defaults)

        for name, des_dict in elec_columns.items():
            des_args = dict(des_dict)
            if name not in default_updated:
                if self.nwbfile.electrodes is None:
                    self.nwbfile.add_electrode_column(
                        name=name, description=des_args["description"], index=des_args["index"]
                    )
                else:
                    # build default junk values for data to force add columns later:
                    combine_data = [channel_property_defaults[found_property_types[name]]] * len(
                        self.nwbfile.electrodes.id
                    )
                    des_args["data"] = combine_data + des_args["data"]
                    elec_columns_append[name] = des_args

        for name in elec_columns_append:
            _ = elec_columns.pop(name)

        for j, channel_id in enumerate(self.recording.get_channel_ids()):
            if channel_id not in nwb_elec_ids:
                electrode_kwargs = dict(default_updated)
                electrode_kwargs.update(id=channel_id)

                # self.recording.get_channel_locations defaults to np.nan if there are none
                location = self.recording.get_channel_locations(channel_ids=channel_id)[0]
                if all([not np.isnan(loc) for loc in location]):
                    # property 'location' of RX channels corresponds to rel_x and rel_ y of NWB electrodes
                    electrode_kwargs.update(dict(rel_x=float(location[0]), rel_y=float(location[1])))

                for name, desc in elec_columns.items():
                    if name == "group_name":
                        group_name = str(desc["data"][j])
                        if group_name != "" and group_name not in self.nwbfile.electrode_groups:
                            warnings.warn(
                                f"Electrode group {group_name} for electrode {channel_id} was not "
                                "found in the nwbfile! Automatically adding."
                            )
                            missing_group_metadata = dict(
                                Ecephys=dict(
                                    ElectrodeGroup=[
                                        dict(
                                            name=group_name,
                                        )
                                    ]
                                )
                            )
                            self.add_electrode_groups(missing_group_metadata=missing_group_metadata)
                        electrode_kwargs.update(
                            dict(group=self.nwbfile.electrode_groups[group_name], group_name=group_name)
                        )
                    elif "data" in desc:
                        electrode_kwargs[name] = desc["data"][j]

                if "group_name" not in elec_columns:
                    group_id = self.recording.get_channel_groups(channel_ids=channel_id)[0]
                    electrode_kwargs.update(
                        dict(group=self.nwbfile.electrode_groups[str(group_id)], group_name=str(group_id))
                    )

                self.nwbfile.add_electrode(**electrode_kwargs)
        # add columns for existing electrodes:
        for col_name, cols_args in elec_columns_append.items():
            self.nwbfile.add_electrode_column(col_name, **cols_args)
        assert (
            self.nwbfile.electrodes is not None
        ), "Unable to form electrode table! Check device, electrode group, and electrode metadata."


    def add_electrical_series(self):
        """
        Auxiliary static method for nwbextractor.

        Adds traces from recording object as ElectricalSeries to nwbfile object.

        Parameters
        ----------
        recording: RecordingExtractor
        nwbfile: NWBFile
            nwb file to which the recording information is to be added
        metadata: dict
            metadata info for constructing the nwb file (optional).
            Should be of the format
                metadata['Ecephys']['ElectricalSeries'] = {'name': my_name,
                                                            'description': my_description}
        buffer_mb: int (optional, defaults to 500MB)
            maximum amount of memory (in MB) to use per iteration of the
            DataChunkIterator (requires traces to be memmap objects)
        use_times: bool (optional, defaults to False)
            If True, the times are saved to the nwb file using recording.frame_to_time(). If False (defualut),
            the sampling rate is used.
        write_as: str (optional, defaults to 'raw')
            How to save the traces data in the nwb file. Options:
            - 'raw' will save it in acquisition
            - 'processed' will save it as FilteredEphys, in a processing module
            - 'lfp' will save it as LFP, in a processing module
        es_key: str (optional)
            Key in metadata dictionary containing metadata info for the specific electrical series
        write_scaled: bool (optional, defaults to True)
            If True, writes the scaled traces (return_scaled=True)
        compression: str (optional, defaults to "gzip")
            Type of compression to use. Valid types are "gzip" and "lzf".
            Set to None to disable all compression.
        compression_opts: int (optional, defaults to 4)
            Only applies to compression="gzip". Controls the level of the GZIP.
        iterate: bool (optional, defaults to True)
            Whether or not to use DataChunkIteration. Highly recommended for large (16+ GB) recordings.

        Missing keys in an element of metadata['Ecephys']['ElectrodeGroup'] will be auto-populated with defaults
        whenever possible.
        """
        if "buffer_mb" not in self._kwargs:
            buffer_mb = 500
        else:
            buffer_mb = int(self._kwargs["buffer_mb"])
        if "use_times" not in self._kwargs:
            use_times = False
        else:
            use_times = bool(self._kwargs["use_times"])
        if "write_as" not in self._kwargs:
            write_as = "raw"
        else:
            write_as = self._kwargs["write_as"]
        if "es_key" not in self._kwargs:
            es_key = None
        else:
            es_key = self._kwargs["es_key"]
        if "write_scaled" not in self._kwargs:
            write_scaled = False
        else:
            write_scaled = bool(self._kwargs["write_scaled"])
        if "compression" not in self._kwargs:
            compression = "gzip"
        else:
            compression = self._kwargs["compression"]
        if "compression_opts" not in self._kwargs:
            compression_opts = None
        else:
            compression_opts = self._kwargs["compression_opts"]
        if "iterate" not in self._kwargs:
            iterate = True
        else:
            iterate = bool(self._kwargs["iterate"])

        if self.nwbfile is not None:
            assert isinstance(self.nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile!"
        assert buffer_mb > 10, "'buffer_mb' should be at least 10MB to ensure data can be chunked!"
        assert compression is None or compression in [
            "gzip",
            "lzf",
        ], "Invalid compression type ({compression})! Choose one of 'gzip', 'lzf', or None."

        if not self.nwbfile.electrodes:
            self.add_electrodes()

        assert write_as in [
            "raw",
            "processed",
            "lfp",
        ], f"'write_as' should be 'raw', 'processed' or 'lfp', but instead received value {write_as}"

        if compression == "gzip":
            if compression_opts is None:
                compression_opts = 4
            else:
                assert compression_opts in range(
                    10
                ), "compression type is 'gzip', but specified compression_opts is not an integer between 0 and 9!"
        elif compression == "lzf" and compression_opts is not None:
            warn(f"compression_opts ({compression_opts}) were passed, but compression type is 'lzf'! Ignoring options.")
            compression_opts = None

        if write_as == "raw":
            eseries_kwargs = dict(
                name="ElectricalSeries_raw",
                description="Raw acquired data",
                comments="Generated from SpikeInterface::NwbRecordingExtractor",
            )
        elif write_as == "processed":
            eseries_kwargs = dict(
                name="ElectricalSeries_processed",
                description="Processed data",
                comments="Generated from SpikeInterface::NwbRecordingExtractor",
            )
            # Check for existing processing module and data interface
            ecephys_mod = check_module(
                nwbfile=self.nwbfile,
                name="ecephys",
                description="Intermediate data from extracellular electrophysiology recordings, e.g., LFP.",
            )
            if "Processed" not in ecephys_mod.data_interfaces:
                ecephys_mod.add(pynwb.ecephys.FilteredEphys(name="Processed"))
        elif write_as == "lfp":
            eseries_kwargs = dict(
                name="ElectricalSeries_lfp",
                description="Processed data - LFP",
                comments="Generated from SpikeInterface::NwbRecordingExtractor",
            )
            # Check for existing processing module and data interface
            ecephys_mod = check_module(
                nwbfile=self.nwbfile,
                name="ecephys",
                description="Intermediate data from extracellular electrophysiology recordings, e.g., LFP.",
            )
            if "LFP" not in ecephys_mod.data_interfaces:
                ecephys_mod.add(pynwb.ecephys.LFP(name="LFP"))

        # If user passed metadata info, overwrite defaults
        if self.metadata is not None and "Ecephys" in self.metadata and es_key is not None:
            assert es_key in self.metadata["Ecephys"], f"metadata['Ecephys'] dictionary does not contain key '{es_key}'"
            eseries_kwargs.update(self.metadata["Ecephys"][es_key])

        # Check for existing names in nwbfile
        if write_as == "raw":
            assert (
                eseries_kwargs["name"] not in self.nwbfile.acquisition
            ), f"Raw ElectricalSeries '{eseries_kwargs['name']}' is already written in the NWBFile!"
        elif write_as == "processed":
            assert (
                eseries_kwargs["name"]
                not in self.nwbfile.processing["ecephys"].data_interfaces["Processed"].electrical_series
            ), f"Processed ElectricalSeries '{eseries_kwargs['name']}' is already written in the NWBFile!"
        elif write_as == "lfp":
            assert (
                eseries_kwargs["name"]
                not in self.nwbfile.processing["ecephys"].data_interfaces["LFP"].electrical_series
            ), f"LFP ElectricalSeries '{eseries_kwargs['name']}' is already written in the NWBFile!"

        # Electrodes table region
        channel_ids = self.recording.get_channel_ids()
        table_ids = [list(self.nwbfile.electrodes.id[:]).index(id) for id in channel_ids]
        electrode_table_region = self.nwbfile.create_electrode_table_region(
            region=table_ids, description="electrode_table_region"
        )
        eseries_kwargs.update(electrodes=electrode_table_region)

        # channels gains - for RecordingExtractor, these are values to cast traces to uV.
        # For nwb, the conversions (gains) cast the data to Volts.
        # To get traces in Volts we take data*channel_conversion*conversion.
        channel_conversion = self.recording.get_channel_gains()
        channel_offset = self.recording.get_channel_offsets()
        unsigned_coercion = channel_offset / channel_conversion
        if not np.all([x.is_integer() for x in unsigned_coercion]):
            raise NotImplementedError(
                "Unable to coerce underlying unsigned data type to signed type, which is currently required for NWB "
                "Schema v2.2.5! Please specify 'write_scaled=True'."
            )
        elif np.any(unsigned_coercion != 0):
            warnings.warn(
                "NWB Schema v2.2.5 does not officially support channel offsets. The data will be converted to a signed "
                "type that does not use offsets."
            )
            unsigned_coercion = unsigned_coercion.astype(int)
        if write_scaled:
            eseries_kwargs.update(conversion=1e-6)
        else:
            if len(np.unique(channel_conversion)) == 1:  # if all gains are equal
                eseries_kwargs.update(conversion=channel_conversion[0] * 1e-6)
            else:
                eseries_kwargs.update(conversion=1e-6)
                eseries_kwargs.update(channel_conversion=channel_conversion)

        trace_dtype = self.recording.get_traces(channel_ids=channel_ids[:1], end_frame=1).dtype
        estimated_memory = trace_dtype.itemsize * self.recording.get_num_channels() * self.recording.get_num_frames()
        if not iterate and psutil.virtual_memory().available <= estimated_memory:
            warn("iteration was disabled, but not enough memory to load traces! Forcing iterate=True.")
            iterate = True
        if iterate:
            if isinstance(self.recording.get_traces(end_frame=5, return_scaled=write_scaled), np.memmap) and np.all(
                channel_offset == 0
            ):
                n_bytes = np.dtype(self.recording.get_dtype()).itemsize
                buffer_size = int(buffer_mb * 1e6) // (self.recording.get_num_channels() * n_bytes)
                ephys_data = DataChunkIterator(
                    data=self.recording.get_traces(return_scaled=write_scaled).T,
                    # nwb standard is time as zero axis
                    buffer_size=buffer_size,
                )
            else:

                def data_generator(recording, channels_ids, unsigned_coercion, write_scaled):
                    for i, ch in enumerate(channels_ids):
                        data = recording.get_traces(channel_ids=[ch], return_scaled=write_scaled)
                        if not write_scaled:
                            data_dtype_name = data.dtype.name
                            if data_dtype_name.startswith("uint"):
                                data_dtype_name = data_dtype_name[1:]  # Retain memory of signed data type
                            data = data + unsigned_coercion[i]
                            data = data.astype(data_dtype_name)
                        yield data.flatten()

                ephys_data = DataChunkIterator(
                    data=data_generator(
                        recording=self.recording,
                        channels_ids=channel_ids,
                        unsigned_coercion=unsigned_coercion,
                        write_scaled=write_scaled,
                    ),
                    iter_axis=1,  # nwb standard is time as zero axis
                    maxshape=(self.recording.get_num_frames(), self.recording.get_num_channels()),
                )
        else:
            ephys_data = self.recording.get_traces(return_scaled=write_scaled).T

        eseries_kwargs.update(data=H5DataIO(ephys_data, compression=compression, compression_opts=compression_opts))
        if not use_times:
            eseries_kwargs.update(
                starting_time=float(self.recording.frame_to_time(0)),
                rate=float(self.recording.get_sampling_frequency()),
            )
        else:
            eseries_kwargs.update(
                timestamps=H5DataIO(
                    self.recording.frame_to_time(np.arange(self.recording.get_num_frames())),
                    compression=compression,
                    compression_opts=compression_opts,
                )
            )

        # Add ElectricalSeries to nwbfile object
        es = pynwb.ecephys.ElectricalSeries(**eseries_kwargs)
        if write_as == "raw":
            self.nwbfile.add_acquisition(es)
        elif write_as == "processed":
            ecephys_mod.data_interfaces["Processed"].add_electrical_series(es)
        elif write_as == "lfp":
            ecephys_mod.data_interfaces["LFP"].add_electrical_series(es)

    def add_units(self):
        """Auxilliary function for write_sorting."""

        if "property_descriptions" not in self._kwargs:
            property_descriptions = None
        else:
            property_descriptions = self._kwargs["property_descriptions"]
        if "use_times" not in self._kwargs:
            use_times = False
        else:
            use_times = bool(self._kwargs["use_times"])
        if "skip_properties" not in self._kwargs:
            skip_properties = None
        else:
            skip_properties = self._kwargs["skip_properties"]
        if "skip_features" not in self._kwargs:
            skip_features = None
        else:
            skip_features = self._kwargs["skip_features"]

        unit_ids = self.sorting.get_unit_ids()
        fs = self.sorting.get_sampling_frequency()
        if fs is None:
            raise ValueError("Writing a SortingExtractor to an NWBFile requires a known sampling frequency!")

        all_properties = set()
        all_features = set()
        for unit_id in unit_ids:
            all_properties.update(self.sorting.get_unit_property_names(unit_id))
            all_features.update(self.sorting.get_unit_spike_feature_names(unit_id))

        if property_descriptions is None:
            property_descriptions = dict(_default_sorting_property_descriptions)
        else:
            property_descriptions = dict(_default_sorting_property_descriptions, **property_descriptions)
        if skip_properties is None:
            skip_properties = []
        if skip_features is None:
            skip_features = []

        if self.nwbfile.units is None:
            # Check that array properties have the same shape across units
            property_shapes = dict()
            for pr in all_properties:
                shapes = []
                for unit_id in unit_ids:
                    if pr in self.sorting.get_unit_property_names(unit_id):
                        prop_value = self.sorting.get_unit_property(unit_id, pr)
                        if isinstance(prop_value, (int, np.integer, float, str, bool)):
                            shapes.append(1)
                        elif isinstance(prop_value, (list, np.ndarray)):
                            if np.array(prop_value).ndim == 1:
                                shapes.append(len(prop_value))
                            else:
                                shapes.append(np.array(prop_value).shape)
                        elif isinstance(prop_value, dict):
                            print(f"Skipping property '{pr}' because dictionaries are not supported.")
                            skip_properties.append(pr)
                            break
                    else:
                        shapes.append(np.nan)
                property_shapes[pr] = shapes

            for pr in property_shapes.keys():
                elems = [elem for elem in property_shapes[pr] if not np.any(np.isnan(elem))]
                if not np.all([elem == elems[0] for elem in elems]):
                    print(f"Skipping property '{pr}' because it has variable size across units.")
                    skip_properties.append(pr)

            write_properties = set(all_properties) - set(skip_properties)
            for pr in write_properties:
                if pr not in property_descriptions:
                    warnings.warn(
                        f"Description for property {pr} not found in property_descriptions. "
                        f"Description for property {pr} not found in property_descriptions. "
                        "Setting description to 'no description'"
                    )
            for pr in write_properties:
                unit_col_args = dict(name=pr, description=property_descriptions.get(pr, "No description."))
                if pr in ["max_channel", "max_electrode"] and self.nwbfile.electrodes is not None:
                    unit_col_args.update(table=self.nwbfile.electrodes)
                self.nwbfile.add_unit_column(**unit_col_args)

            for unit_id in unit_ids:
                unit_kwargs = dict()
                if use_times:
                    spkt = self.sorting.frame_to_time(self.sorting.get_unit_spike_train(unit_id=unit_id))
                else:
                    spkt = self.sorting.get_unit_spike_train(unit_id=unit_id) / self.sorting.get_sampling_frequency()
                for pr in write_properties:
                    if pr in self.sorting.get_unit_property_names(unit_id):
                        prop_value = self.sorting.get_unit_property(unit_id, pr)
                        unit_kwargs.update({pr: prop_value})
                    else:  # Case of missing data for this unit and this property
                        unit_kwargs.update({pr: np.nan})
                self.nwbfile.add_unit(id=int(unit_id), spike_times=spkt, **unit_kwargs)

            # Check that multidimensional features have the same shape across units
            feature_shapes = dict()
            for ft in all_features:
                shapes = []
                for unit_id in unit_ids:
                    if ft in self.sorting.get_unit_spike_feature_names(unit_id):
                        feat_value = self.sorting.get_unit_spike_features(unit_id, ft)
                        if isinstance(feat_value[0], (int, np.integer, float, str, bool)):
                            break
                        elif isinstance(feat_value[0], (list, np.ndarray)):  # multidimensional features
                            if np.array(feat_value).ndim > 1:
                                shapes.append(np.array(feat_value).shape)
                                feature_shapes[ft] = shapes
                        elif isinstance(feat_value[0], dict):
                            print(f"Skipping feature '{ft}' because dictionaries are not supported.")
                            skip_features.append(ft)
                            break
                    else:
                        print(f"Skipping feature '{ft}' because not share across all units.")
                        skip_features.append(ft)
                        break

            nspikes = {k: get_num_spikes(self.nwbfile.units, int(k)) for k in unit_ids}

            for ft in feature_shapes.keys():
                # skip first dimension (num_spikes) when comparing feature shape
                if not np.all([elem[1:] == feature_shapes[ft][0][1:] for elem in feature_shapes[ft]]):
                    print(f"Skipping feature '{ft}' because it has variable size across units.")
                    skip_features.append(ft)

            for ft in set(all_features) - set(skip_features):
                values = []
                if not ft.endswith("_idxs"):
                    for unit_id in self.sorting.get_unit_ids():
                        feat_vals = self.sorting.get_unit_spike_features(unit_id, ft)

                        if len(feat_vals) < nspikes[unit_id]:
                            skip_features.append(ft)
                            print(f"Skipping feature '{ft}' because it is not defined for all spikes.")
                            break
                            # this means features are available for a subset of spikes
                            # all_feat_vals = np.array([np.nan] * nspikes[unit_id])
                            # feature_idxs = sorting.get_unit_spike_features(unit_id, feat_name + '_idxs')
                            # all_feat_vals[feature_idxs] = feat_vals
                        else:
                            all_feat_vals = feat_vals
                        values.append(all_feat_vals)

                    flatten_vals = [item for sublist in values for item in sublist]
                    nspks_list = [sp for sp in nspikes.values()]
                    spikes_index = np.cumsum(nspks_list).astype("int64")
                    if ft in self.nwbfile.units:  # If property already exists, skip it
                        warnings.warn(f"Feature {ft} already present in units table, skipping it")
                        continue
                    set_dynamic_table_property(
                        dynamic_table=self.nwbfile.units,
                        row_ids=[int(k) for k in unit_ids],
                        property_name=ft,
                        values=flatten_vals,
                        index=spikes_index,
                    )
        else:
            warnings.warn("The nwbfile already contains units. These units will not be over-written.")

    def add_epochs(self):
        """
        Auxiliary static method for nwbextractor.

        Adds epochs from recording object to nwbfile object.

        """
        if self.nwbfile is not None:
            assert isinstance(self.nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"

        # add/update epochs
        for epoch_name in self.recording.get_epoch_names():
            epoch = self.recording.get_epoch_info(epoch_name)
            if self.nwbfile.epochs is None:
                self.nwbfile.add_epoch(
                    start_time=self.recording.frame_to_time(epoch["start_frame"]),
                    stop_time=self.recording.frame_to_time(epoch["end_frame"] - 1),
                    tags=epoch_name,
                )
            else:
                if [epoch_name] in self.nwbfile.epochs["tags"][:]:
                    ind = self.nwbfile.epochs["tags"][:].index([epoch_name])
                    self.nwbfile.epochs["start_time"].data[ind] = self.recording.frame_to_time(epoch["start_frame"])
                    self.nwbfile.epochs["stop_time"].data[ind] = self.recording.frame_to_time(epoch["end_frame"])
                else:
                    self.nwbfile.add_epoch(
                        start_time=self.recording.frame_to_time(epoch["start_frame"]),
                        stop_time=self.recording.frame_to_time(epoch["end_frame"]),
                        tags=epoch_name,
                    )


def get_num_spikes(units_table, unit_id):
    """Return the number of spikes for chosen unit."""
    ids = np.array(units_table.id[:])
    indexes = np.where(ids == unit_id)[0]
    if not len(indexes):
        raise ValueError(f"{unit_id} is an invalid unit_id. Valid ids: {ids}.")
    index = indexes[0]
    if index == 0:
        return units_table["spike_times_index"].data[index]
    else:
        return units_table["spike_times_index"].data[index] - units_table["spike_times_index"].data[index - 1]


#
# def get_nwb_metadata(recording: se.RecordingExtractor, metadata: dict = None):
#     """
#     Return default metadata for all recording fields.
#
#     Parameters
#     ----------
#     recording: RecordingExtractor
#     metadata: dict
#         metadata info for constructing the nwb file (optional).
#     """
#     metadata = dict(
#         NWBFile=dict(
#             session_description="Auto-generated by NwbRecordingExtractor without description.",
#             identifier=str(uuid.uuid4()),
#             session_start_time=datetime(1970, 1, 1),
#         ),
#         Ecephys=dict(
#             Device=[dict(name="Device", description="no description")],
#             ElectrodeGroup=[
#                 dict(name=str(gn), description="no description", location="unknown", device="Device")
#                 for gn in np.unique(recording.get_channel_groups())
#             ],
#         ),
#     )
#     return metadata
#
#
# def add_electrode_groups(recording: se.RecordingExtractor, nwbfile=None, metadata: dict = None):
#     """
#     Auxiliary static method for nwbextractor.
#
#     Adds electrode group information to nwbfile object.
#     Will always ensure nwbfile has at least one electrode group.
#     Will auto-generate a linked device if the specified name does not exist in the nwbfile.
#
#     Parameters
#     ----------
#     recording: RecordingExtractor
#     nwbfile: NWBFile
#         nwb file to which the recording information is to be added
#     metadata: dict
#         metadata info for constructing the nwb file (optional).
#         Should be of the format
#             metadata['Ecephys']['ElectrodeGroup'] = [{'name': my_name,
#                                                         'description': my_description,
#                                                         'location': electrode_location,
#                                                         'device_name': my_device_name}, ...]
#
#     Missing keys in an element of metadata['Ecephys']['ElectrodeGroup'] will be auto-populated with defaults.
#
#     Group names set by RecordingExtractor channel properties will also be included with passed metadata,
#     but will only use default description and location.
#     """
#     if nwbfile is not None:
#         assert isinstance(nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"
#
#     if len(nwbfile.devices) == 0:
#         warnings.warn("When adding ElectrodeGroup, no Devices were found on nwbfile. Creating a Device now...")
#         add_devices(recording=recording, nwbfile=nwbfile, metadata=metadata)
#
#     if metadata is None:
#         metadata = dict()
#
#     if "Ecephys" not in metadata:
#         metadata["Ecephys"] = dict()
#
#     defaults = [
#         dict(
#             name=str(group_id),
#             description="no description",
#             location="unknown",
#             device=[i.name for i in nwbfile.devices.values()][0],
#         )
#         for group_id in np.unique(recording.get_channel_groups())
#     ]
#
#     if "ElectrodeGroup" not in metadata["Ecephys"]:
#         metadata["Ecephys"]["ElectrodeGroup"] = defaults
#
#     assert all(
#         [isinstance(x, dict) for x in metadata["Ecephys"]["ElectrodeGroup"]]
#     ), "Expected metadata['Ecephys']['ElectrodeGroup'] to be a list of dictionaries!"
#
#     for grp in metadata["Ecephys"]["ElectrodeGroup"]:
#         if grp.get("name", defaults[0]["name"]) not in nwbfile.electrode_groups:
#             device_name = grp.get("device", defaults[0]["device"])
#             if device_name not in nwbfile.devices:
#                 new_device_metadata = dict(Ecephys=dict(Device=[dict(name=device_name)]))
#                 add_devices(recording, nwbfile, metadata=new_device_metadata)
#                 warnings.warn(
#                     f"Device '{device_name}' not detected in "
#                     "attempted link to electrode group! Automatically generating."
#                 )
#             electrode_group_kwargs = dict(defaults[0], **grp)
#             electrode_group_kwargs.update(device=nwbfile.devices[device_name])
#             nwbfile.create_electrode_group(**electrode_group_kwargs)
#
#     if not nwbfile.electrode_groups:
#         device_name = list(nwbfile.devices.keys())[0]
#         device = nwbfile.devices[device_name]
#         if len(nwbfile.devices) > 1:
#             warnings.warn(
#                 "More than one device found when adding electrode group "
#                 f"via channel properties: using device '{device_name}'. To use a "
#                 "different device, indicate it the metadata argument."
#             )
#
#         electrode_group_kwargs = dict(defaults[0])
#         electrode_group_kwargs.update(device=device)
#         for grp_name in np.unique(recording.get_channel_groups()).tolist():
#             electrode_group_kwargs.update(name=str(grp_name))
#             nwbfile.create_electrode_group(**electrode_group_kwargs)
#
#
# def add_electrodes(recording: se.RecordingExtractor, nwbfile=None, metadata: dict = None, exclude: tuple = ()):
#     """
#     Auxiliary static method for nwbextractor.
#
#     Adds channels from recording object as electrodes to nwbfile object.
#
#     Parameters
#     ----------
#     recording: RecordingExtractor
#     nwbfile: NWBFile
#         nwb file to which the recording information is to be added
#     metadata: dict
#         metadata info for constructing the nwb file (optional).
#         Should be of the format
#             metadata['Ecephys']['Electrodes'] = [{'name': my_name,
#                                                     'description': my_description,
#                                                     'data': [my_electrode_data]}, ...]
#         where each dictionary corresponds to a column in the Electrodes table and [my_electrode_data] is a list in
#         one-to-one correspondence with the nwbfile electrode ids and RecordingExtractor channel ids.
#     exclude: tuple
#         An iterable containing the string names of channel properties in the RecordingExtractor
#         object to ignore when writing to the NWBFile.
#
#     Missing keys in an element of metadata['Ecephys']['ElectrodeGroup'] will be auto-populated with defaults
#     whenever possible.
#
#     If 'my_name' is set to one of the required fields for nwbfile
#     electrodes (id, x, y, z, imp, loccation, filtering, group_name),
#     then the metadata will override their default values.
#
#     Setting 'my_name' to metadata field 'group' is not supported as the linking to
#     nwbfile.electrode_groups is handled automatically; please specify the string 'group_name' in this case.
#
#     If no group information is passed via metadata, automatic linking to existing electrode groups,
#     possibly including the default, will occur.
#     """
#     if nwbfile.electrodes is not None:
#         ids_absent = [id not in nwbfile.electrodes.id for id in recording.get_channel_ids()]
#         if not all(ids_absent):
#             warnings.warn("cannot create electrodes for this recording as ids already exist")
#             return
#
#     if nwbfile is not None:
#         assert isinstance(nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"
#     if nwbfile.electrode_groups is None or len(nwbfile.electrode_groups) == 0:
#         add_electrode_groups(recording, nwbfile, metadata)
#     # For older versions of pynwb, we need to manually add these columns
#     if distutils.version.LooseVersion(pynwb.__version__) < "1.3.0":
#         if nwbfile.electrodes is None or "rel_x" not in nwbfile.electrodes.colnames:
#             nwbfile.add_electrode_column("rel_x", "x position of electrode in electrode group")
#         if nwbfile.electrodes is None or "rel_y" not in nwbfile.electrodes.colnames:
#             nwbfile.add_electrode_column("rel_y", "y position of electrode in electrode group")
#
#     defaults = dict(
#         x=np.nan,
#         y=np.nan,
#         z=np.nan,
#         # There doesn't seem to be a canonical default for impedence, if missing.
#         # The NwbRecordingExtractor follows the -1.0 convention, other scripts sometimes use np.nan
#         imp=-1.0,
#         location="unknown",
#         filtering="none",
#         group_name="0",
#     )
#     if metadata is None:
#         metadata = dict(Ecephys=dict())
#
#     if "Ecephys" not in metadata:
#         metadata["Ecephys"] = dict()
#
#     if "Electrodes" not in metadata["Ecephys"]:
#         metadata["Ecephys"]["Electrodes"] = []
#
#     assert all(
#         [
#             isinstance(x, dict) and set(x.keys()) == set(["name", "description"])
#             for x in metadata["Ecephys"]["Electrodes"]
#         ]
#     ), "Expected metadata['Ecephys']['Electrodes'] to be a list of dictionaries, containing the keys 'name' and 'description'"
#     assert all(
#         [x["name"] != "group" for x in metadata["Ecephys"]["Electrodes"]]
#     ), "Passing metadata field 'group' is deprecated; pass group_name instead!"
#
#     if nwbfile.electrodes is None:
#         nwb_elec_ids = []
#     else:
#         nwb_elec_ids = nwbfile.electrodes.id.data[:]
#
#     elec_columns = defaultdict(dict)  # dict(name: dict(description='',data=data, index=False))
#     elec_columns_append = defaultdict(dict)
#     property_names = set()
#     for chan_id in recording.get_channel_ids():
#         for i in recording.get_channel_property_names(channel_id=chan_id):
#             property_names.add(i)
#
#     # property 'brain_area' of RX channels corresponds to 'location' of NWB electrodes
#     exclude_names = set(["location", "group"] + list(exclude))
#
#     channel_property_defaults = {list: [], np.ndarray: np.array(np.nan), str: "", Real: np.nan}
#     found_property_types = {prop: Real for prop in property_names}
#
#     for prop in property_names:
#         prop_skip = False
#         if prop not in exclude_names:
#             data = []
#             prop_chan_count = 0
#             # build data:
#             for chan_id in recording.get_channel_ids():
#                 if prop in recording.get_channel_property_names(channel_id=chan_id):
#                     prop_chan_count += 1
#                     chan_data = recording.get_channel_property(channel_id=chan_id, property_name=prop)
#                     # find the type and store (only when the first channel with given property is found):
#                     if prop_chan_count == 1:
#                         proptype = [
#                             proptype for proptype in channel_property_defaults if isinstance(chan_data, proptype)
#                         ]
#                         if len(proptype) > 0:
#                             found_property_types[prop] = proptype[0]
#                             # cast as float if any number:
#                             if found_property_types[prop] == Real:
#                                 chan_data = np.float(chan_data)
#                             # update data if wrong datatype items filled prior:
#                             if len(data) > 0 and not isinstance(data[-1], found_property_types[prop]):
#                                 data = [channel_property_defaults[found_property_types[prop]]] * len(data)
#                         else:
#                             prop_skip = True  # skip storing that property if not of default type
#                             break
#                     data.append(chan_data)
#                 else:
#                     data.append(channel_property_defaults[found_property_types[prop]])
#             # store data after build:
#             if not prop_skip:
#                 index = found_property_types[prop] == ArrayType
#                 prop_name_new = "location" if prop == "brain_area" else prop
#                 found_property_types[prop_name_new] = found_property_types.pop(prop)
#                 elec_columns[prop_name_new].update(description=prop_name_new, data=data, index=index)
#
#     for x in metadata["Ecephys"]["Electrodes"]:
#         elec_columns[x["name"]]["description"] = x["description"]
#         if x["name"] not in list(elec_columns):
#             raise ValueError(f'"{x["name"]}" not a property of se object')
#
#     # updating default arguments if electrodes table already present:
#     default_updated = dict()
#     if nwbfile.electrodes is not None:
#         for colname in nwbfile.electrodes.colnames:
#             if colname != "group":
#                 samp_data = nwbfile.electrodes[colname].data[0]
#                 default_datatype = [
#                     proptype for proptype in channel_property_defaults if isinstance(samp_data, proptype)
#                 ][0]
#                 default_updated.update({colname: channel_property_defaults[default_datatype]})
#     default_updated.update(defaults)
#
#     for name, des_dict in elec_columns.items():
#         des_args = dict(des_dict)
#         if name not in default_updated:
#             if nwbfile.electrodes is None:
#                 nwbfile.add_electrode_column(name=name, description=des_args["description"], index=des_args["index"])
#             else:
#                 # build default junk values for data to force add columns later:
#                 combine_data = [channel_property_defaults[found_property_types[name]]] * len(nwbfile.electrodes.id)
#                 des_args["data"] = combine_data + des_args["data"]
#                 elec_columns_append[name] = des_args
#
#     for name in elec_columns_append:
#         _ = elec_columns.pop(name)
#
#     for j, channel_id in enumerate(recording.get_channel_ids()):
#         if channel_id not in nwb_elec_ids:
#             electrode_kwargs = dict(default_updated)
#             electrode_kwargs.update(id=channel_id)
#
#             # recording.get_channel_locations defaults to np.nan if there are none
#             location = recording.get_channel_locations(channel_ids=channel_id)[0]
#             if all([not np.isnan(loc) for loc in location]):
#                 # property 'location' of RX channels corresponds to rel_x and rel_ y of NWB electrodes
#                 electrode_kwargs.update(dict(rel_x=float(location[0]), rel_y=float(location[1])))
#
#             for name, desc in elec_columns.items():
#                 if name == "group_name":
#                     group_name = str(desc["data"][j])
#                     if group_name != "" and group_name not in nwbfile.electrode_groups:
#                         warnings.warn(
#                             f"Electrode group {group_name} for electrode {channel_id} was not "
#                             "found in the nwbfile! Automatically adding."
#                         )
#                         missing_group_metadata = dict(
#                             Ecephys=dict(
#                                 ElectrodeGroup=[
#                                     dict(
#                                         name=group_name,
#                                     )
#                                 ]
#                             )
#                         )
#                         add_electrode_groups(recording, nwbfile, missing_group_metadata)
#                     electrode_kwargs.update(dict(group=nwbfile.electrode_groups[group_name], group_name=group_name))
#                 elif "data" in desc:
#                     electrode_kwargs[name] = desc["data"][j]
#
#             if "group_name" not in elec_columns:
#                 group_id = recording.get_channel_groups(channel_ids=channel_id)[0]
#                 electrode_kwargs.update(dict(group=nwbfile.electrode_groups[str(group_id)], group_name=str(group_id)))
#
#             nwbfile.add_electrode(**electrode_kwargs)
#     # add columns for existing electrodes:
#     for col_name, cols_args in elec_columns_append.items():
#         nwbfile.add_electrode_column(col_name, **cols_args)
#     assert (
#         nwbfile.electrodes is not None
#     ), "Unable to form electrode table! Check device, electrode group, and electrode metadata."
#
#
# def add_electrical_series(
#     recording: se.RecordingExtractor,
#     nwbfile=None,
#     metadata: dict = None,
#     buffer_mb: int = 500,
#     use_times: bool = False,
#     write_as: str = "raw",
#     es_key: str = None,
#     write_scaled: bool = False,
#     compression: Optional[str] = "gzip",
#     compression_opts: Optional[int] = None,
#     iterate: bool = True,
# ):
#     """
#     Auxiliary static method for nwbextractor.
#
#     Adds traces from recording object as ElectricalSeries to nwbfile object.
#
#     Parameters
#     ----------
#     recording: RecordingExtractor
#     nwbfile: NWBFile
#         nwb file to which the recording information is to be added
#     metadata: dict
#         metadata info for constructing the nwb file (optional).
#         Should be of the format
#             metadata['Ecephys']['ElectricalSeries'] = {'name': my_name,
#                                                         'description': my_description}
#     buffer_mb: int (optional, defaults to 500MB)
#         maximum amount of memory (in MB) to use per iteration of the
#         DataChunkIterator (requires traces to be memmap objects)
#     use_times: bool (optional, defaults to False)
#         If True, the times are saved to the nwb file using recording.frame_to_time(). If False (defualut),
#         the sampling rate is used.
#     write_as: str (optional, defaults to 'raw')
#         How to save the traces data in the nwb file. Options:
#         - 'raw' will save it in acquisition
#         - 'processed' will save it as FilteredEphys, in a processing module
#         - 'lfp' will save it as LFP, in a processing module
#     es_key: str (optional)
#         Key in metadata dictionary containing metadata info for the specific electrical series
#     write_scaled: bool (optional, defaults to True)
#         If True, writes the scaled traces (return_scaled=True)
#     compression: str (optional, defaults to "gzip")
#         Type of compression to use. Valid types are "gzip" and "lzf".
#         Set to None to disable all compression.
#     compression_opts: int (optional, defaults to 4)
#         Only applies to compression="gzip". Controls the level of the GZIP.
#     iterate: bool (optional, defaults to True)
#         Whether or not to use DataChunkIteration. Highly recommended for large (16+ GB) recordings.
#
#     Missing keys in an element of metadata['Ecephys']['ElectrodeGroup'] will be auto-populated with defaults
#     whenever possible.
#     """
#     if nwbfile is not None:
#         assert isinstance(nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile!"
#     assert buffer_mb > 10, "'buffer_mb' should be at least 10MB to ensure data can be chunked!"
#     assert compression is None or compression in [
#         "gzip",
#         "lzf",
#     ], "Invalid compression type ({compression})! Choose one of 'gzip', 'lzf', or None."
#
#     if not nwbfile.electrodes:
#         add_electrodes(recording, nwbfile, metadata)
#
#     assert write_as in [
#         "raw",
#         "processed",
#         "lfp",
#     ], f"'write_as' should be 'raw', 'processed' or 'lfp', but instead received value {write_as}"
#
#     if compression == "gzip":
#         if compression_opts is None:
#             compression_opts = 4
#         else:
#             assert compression_opts in range(
#                 10
#             ), "compression type is 'gzip', but specified compression_opts is not an integer between 0 and 9!"
#     elif compression == "lzf" and compression_opts is not None:
#         warn(f"compression_opts ({compression_opts}) were passed, but compression type is 'lzf'! Ignoring options.")
#         compression_opts = None
#
#     if write_as == "raw":
#         eseries_kwargs = dict(
#             name="ElectricalSeries_raw",
#             description="Raw acquired data",
#             comments="Generated from SpikeInterface::NwbRecordingExtractor",
#         )
#     elif write_as == "processed":
#         eseries_kwargs = dict(
#             name="ElectricalSeries_processed",
#             description="Processed data",
#             comments="Generated from SpikeInterface::NwbRecordingExtractor",
#         )
#         # Check for existing processing module and data interface
#         ecephys_mod = check_module(
#             nwbfile=nwbfile,
#             name="ecephys",
#             description="Intermediate data from extracellular electrophysiology recordings, e.g., LFP.",
#         )
#         if "Processed" not in ecephys_mod.data_interfaces:
#             ecephys_mod.add(pynwb.ecephys.FilteredEphys(name="Processed"))
#     elif write_as == "lfp":
#         eseries_kwargs = dict(
#             name="ElectricalSeries_lfp",
#             description="Processed data - LFP",
#             comments="Generated from SpikeInterface::NwbRecordingExtractor",
#         )
#         # Check for existing processing module and data interface
#         ecephys_mod = check_module(
#             nwbfile=nwbfile,
#             name="ecephys",
#             description="Intermediate data from extracellular electrophysiology recordings, e.g., LFP.",
#         )
#         if "LFP" not in ecephys_mod.data_interfaces:
#             ecephys_mod.add(pynwb.ecephys.LFP(name="LFP"))
#
#     # If user passed metadata info, overwrite defaults
#     if metadata is not None and "Ecephys" in metadata and es_key is not None:
#         assert es_key in metadata["Ecephys"], f"metadata['Ecephys'] dictionary does not contain key '{es_key}'"
#         eseries_kwargs.update(metadata["Ecephys"][es_key])
#
#     # Check for existing names in nwbfile
#     if write_as == "raw":
#         assert (
#             eseries_kwargs["name"] not in nwbfile.acquisition
#         ), f"Raw ElectricalSeries '{eseries_kwargs['name']}' is already written in the NWBFile!"
#     elif write_as == "processed":
#         assert (
#             eseries_kwargs["name"] not in nwbfile.processing["ecephys"].data_interfaces["Processed"].electrical_series
#         ), f"Processed ElectricalSeries '{eseries_kwargs['name']}' is already written in the NWBFile!"
#     elif write_as == "lfp":
#         assert (
#             eseries_kwargs["name"] not in nwbfile.processing["ecephys"].data_interfaces["LFP"].electrical_series
#         ), f"LFP ElectricalSeries '{eseries_kwargs['name']}' is already written in the NWBFile!"
#
#     # Electrodes table region
#     channel_ids = recording.get_channel_ids()
#     table_ids = [list(nwbfile.electrodes.id[:]).index(id) for id in channel_ids]
#     electrode_table_region = nwbfile.create_electrode_table_region(
#         region=table_ids, description="electrode_table_region"
#     )
#     eseries_kwargs.update(electrodes=electrode_table_region)
#
#     # channels gains - for RecordingExtractor, these are values to cast traces to uV.
#     # For nwb, the conversions (gains) cast the data to Volts.
#     # To get traces in Volts we take data*channel_conversion*conversion.
#     channel_conversion = recording.get_channel_gains()
#     channel_offset = recording.get_channel_offsets()
#     unsigned_coercion = channel_offset / channel_conversion
#     if not np.all([x.is_integer() for x in unsigned_coercion]):
#         raise NotImplementedError(
#             "Unable to coerce underlying unsigned data type to signed type, which is currently required for NWB "
#             "Schema v2.2.5! Please specify 'write_scaled=True'."
#         )
#     elif np.any(unsigned_coercion != 0):
#         warnings.warn(
#             "NWB Schema v2.2.5 does not officially support channel offsets. The data will be converted to a signed "
#             "type that does not use offsets."
#         )
#         unsigned_coercion = unsigned_coercion.astype(int)
#     if write_scaled:
#         eseries_kwargs.update(conversion=1e-6)
#     else:
#         if len(np.unique(channel_conversion)) == 1:  # if all gains are equal
#             eseries_kwargs.update(conversion=channel_conversion[0] * 1e-6)
#         else:
#             eseries_kwargs.update(conversion=1e-6)
#             eseries_kwargs.update(channel_conversion=channel_conversion)
#
#     trace_dtype = recording.get_traces(channel_ids=channel_ids[:1], end_frame=1).dtype
#     estimated_memory = trace_dtype.itemsize * recording.get_num_channels() * recording.get_num_frames()
#     if not iterate and psutil.virtual_memory().available <= estimated_memory:
#         warn("iteration was disabled, but not enough memory to load traces! Forcing iterate=True.")
#         iterate = True
#     if iterate:
#         if isinstance(recording.get_traces(end_frame=5, return_scaled=write_scaled), np.memmap) and np.all(
#             channel_offset == 0
#         ):
#             n_bytes = np.dtype(recording.get_dtype()).itemsize
#             buffer_size = int(buffer_mb * 1e6) // (recording.get_num_channels() * n_bytes)
#             ephys_data = DataChunkIterator(
#                 data=recording.get_traces(return_scaled=write_scaled).T,  # nwb standard is time as zero axis
#                 buffer_size=buffer_size,
#             )
#         else:
#
#             def data_generator(recording, channels_ids, unsigned_coercion, write_scaled):
#                 for i, ch in enumerate(channels_ids):
#                     data = recording.get_traces(channel_ids=[ch], return_scaled=write_scaled)
#                     if not write_scaled:
#                         data_dtype_name = data.dtype.name
#                         if data_dtype_name.startswith("uint"):
#                             data_dtype_name = data_dtype_name[1:]  # Retain memory of signed data type
#                         data = data + unsigned_coercion[i]
#                         data = data.astype(data_dtype_name)
#                     yield data.flatten()
#
#             ephys_data = DataChunkIterator(
#                 data=data_generator(
#                     recording=recording,
#                     channels_ids=channel_ids,
#                     unsigned_coercion=unsigned_coercion,
#                     write_scaled=write_scaled,
#                 ),
#                 iter_axis=1,  # nwb standard is time as zero axis
#                 maxshape=(recording.get_num_frames(), recording.get_num_channels()),
#             )
#     else:
#         ephys_data = recording.get_traces(return_scaled=write_scaled).T
#
#     eseries_kwargs.update(data=H5DataIO(ephys_data, compression=compression, compression_opts=compression_opts))
#     if not use_times:
#         eseries_kwargs.update(
#             starting_time=float(recording.frame_to_time(0)), rate=float(recording.get_sampling_frequency())
#         )
#     else:
#         eseries_kwargs.update(
#             timestamps=H5DataIO(
#                 recording.frame_to_time(np.arange(recording.get_num_frames())),
#                 compression=compression,
#                 compression_opts=compression_opts,
#             )
#         )
#
#     # Add ElectricalSeries to nwbfile object
#     es = pynwb.ecephys.ElectricalSeries(**eseries_kwargs)
#     if write_as == "raw":
#         nwbfile.add_acquisition(es)
#     elif write_as == "processed":
#         ecephys_mod.data_interfaces["Processed"].add_electrical_series(es)
#     elif write_as == "lfp":
#         ecephys_mod.data_interfaces["LFP"].add_electrical_series(es)
#
#
# def add_epochs(recording: se.RecordingExtractor, nwbfile=None, metadata: dict = None):
#     """
#     Auxiliary static method for nwbextractor.
#
#     Adds epochs from recording object to nwbfile object.
#
#     Parameters
#     ----------
#     recording: RecordingExtractor
#     nwbfile: NWBFile
#         nwb file to which the recording information is to be added
#     metadata: dict
#         metadata info for constructing the nwb file (optional).
#     """
#     if nwbfile is not None:
#         assert isinstance(nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"
#
#     # add/update epochs
#     for epoch_name in recording.get_epoch_names():
#         epoch = recording.get_epoch_info(epoch_name)
#         if nwbfile.epochs is None:
#             nwbfile.add_epoch(
#                 start_time=recording.frame_to_time(epoch["start_frame"]),
#                 stop_time=recording.frame_to_time(epoch["end_frame"] - 1),
#                 tags=epoch_name,
#             )
#         else:
#             if [epoch_name] in nwbfile.epochs["tags"][:]:
#                 ind = nwbfile.epochs["tags"][:].index([epoch_name])
#                 nwbfile.epochs["start_time"].data[ind] = recording.frame_to_time(epoch["start_frame"])
#                 nwbfile.epochs["stop_time"].data[ind] = recording.frame_to_time(epoch["end_frame"])
#             else:
#                 nwbfile.add_epoch(
#                     start_time=recording.frame_to_time(epoch["start_frame"]),
#                     stop_time=recording.frame_to_time(epoch["end_frame"]),
#                     tags=epoch_name,
#                 )
#
#
# def add_all_to_nwbfile(
#     recording: se.RecordingExtractor,
#     nwbfile=None,
#     buffer_mb: int = 500,
#     use_times: bool = False,
#     metadata: dict = None,
#     write_as: str = "raw",
#     es_key: str = None,
#     write_scaled: bool = False,
#     compression: Optional[str] = "gzip",
#     iterate: bool = True,
# ):
#     """
#     Auxiliary static method for nwbextractor.
#
#     Adds all recording related information from recording object and metadata to the nwbfile object.
#
#     Parameters
#     ----------
#     recording: RecordingExtractor
#     nwbfile: NWBFile
#         nwb file to which the recording information is to be added
#     buffer_mb: int (optional, defaults to 500MB)
#         maximum amount of memory (in MB) to use per iteration of the
#         DataChunkIterator (requires traces to be memmap objects)
#     use_times: bool
#         If True, the times are saved to the nwb file using recording.frame_to_time(). If False (defualut),
#         the sampling rate is used.
#     metadata: dict
#         metadata info for constructing the nwb file (optional).
#         Check the auxiliary function docstrings for more information
#         about metadata format.
#     write_as: str (optional, defaults to 'raw')
#         How to save the traces data in the nwb file. Options:
#         - 'raw' will save it in acquisition
#         - 'processed' will save it as FilteredEphys, in a processing module
#         - 'lfp' will save it as LFP, in a processing module
#     es_key: str (optional)
#         Key in metadata dictionary containing metadata info for the specific electrical series
#     write_scaled: bool (optional, defaults to True)
#         If True, writes the scaled traces (return_scaled=True)
#     compression: str (optional, defaults to "gzip")
#         Type of compression to use. Valid types are "gzip" and "lzf".
#         Set to None to disable all compression.
#     compression_opts: int (optional, defaults to 4)
#         Only applies to compression="gzip". Controls the level of the GZIP.
#     iterate: bool (optional, defaults to True)
#         Whether or not to use DataChunkIteration. Highly recommended for large (16+ GB) recordings.
#     """
#     if nwbfile is not None:
#         assert isinstance(nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"
#
#     add_devices(recording=recording, nwbfile=nwbfile, metadata=metadata)
#
#     add_electrode_groups(recording=recording, nwbfile=nwbfile, metadata=metadata)
#
#     add_electrodes(
#         recording=recording,
#         nwbfile=nwbfile,
#         metadata=metadata,
#     )
#
#     add_electrical_series(
#         recording=recording,
#         nwbfile=nwbfile,
#         buffer_mb=buffer_mb,
#         use_times=use_times,
#         metadata=metadata,
#         write_as=write_as,
#         es_key=es_key,
#         write_scaled=write_scaled,
#         compression=compression,
#         iterate=iterate,
#     )
#
#     add_epochs(recording=recording, nwbfile=nwbfile, metadata=metadata)
#
#
# def write_recording(
#     recording: se.RecordingExtractor,
#     save_path: PathType = None,
#     overwrite: bool = False,
#     nwbfile=None,
#     buffer_mb: int = 500,
#     use_times: bool = False,
#     metadata: dict = None,
#     write_as: str = "raw",
#     es_key: str = None,
#     write_scaled: bool = False,
#     compression: Optional[str] = "gzip",
#     iterate: bool = True,
# ):
#     """
#     Primary method for writing a RecordingExtractor object to an NWBFile.
#
#     Parameters
#     ----------
#     recording: RecordingExtractor
#     save_path: PathType
#         Required if an nwbfile is not passed. Must be the path to the nwbfile
#         being appended, otherwise one is created and written.
#     overwrite: bool
#         If using save_path, whether or not to overwrite the NWBFile if it already exists.
#     nwbfile: NWBFile
#         Required if a save_path is not specified. If passed, this function
#         will fill the relevant fields within the nwbfile. E.g., calling
#         spikeextractors.NwbRecordingExtractor.write_recording(
#             my_recording_extractor, my_nwbfile
#         )
#         will result in the appropriate changes to the my_nwbfile object.
#     buffer_mb: int (optional, defaults to 500MB)
#         maximum amount of memory (in MB) to use per iteration of the
#         DataChunkIterator (requires traces to be memmap objects)
#     use_times: bool
#         If True, the times are saved to the nwb file using recording.frame_to_time(). If False (defualut),
#         the sampling rate is used.
#     metadata: dict
#         metadata info for constructing the nwb file (optional). Should be
#         of the format
#             metadata['Ecephys'] = {}
#         with keys of the forms
#             metadata['Ecephys']['Device'] = [{'name': my_name,
#                                                 'description': my_description}, ...]
#             metadata['Ecephys']['ElectrodeGroup'] = [{'name': my_name,
#                                                         'description': my_description,
#                                                         'location': electrode_location,
#                                                         'device': my_device_name}, ...]
#             metadata['Ecephys']['Electrodes'] = [{'name': my_name,
#                                                     'description': my_description,
#                                                     'data': [my_electrode_data]}, ...]
#             metadata['Ecephys']['ElectricalSeries'] = {'name': my_name,
#                                                         'description': my_description}
#     write_as: str (optional, defaults to 'raw')
#         How to save the traces data in the nwb file. Options:
#         - 'raw' will save it in acquisition
#         - 'processed' will save it as FilteredEphys, in a processing module
#         - 'lfp' will save it as LFP, in a processing module
#     es_key: str (optional)
#         Key in metadata dictionary containing metadata info for the specific electrical series
#     write_scaled: bool (optional, defaults to True)
#         If True, writes the scaled traces (return_scaled=True)
#     compression: str (optional, defaults to "gzip")
#         Type of compression to use. Valid types are "gzip" and "lzf".
#         Set to None to disable all compression.
#     compression_opts: int (optional, defaults to 4)
#         Only applies to compression="gzip". Controls the level of the GZIP.
#     iterate: bool (optional, defaults to True)
#         Whether or not to use DataChunkIteration. Highly recommended for large (16+ GB) recordings.
#     """
#     if nwbfile is not None:
#         assert isinstance(nwbfile, pynwb.NWBFile), "'nwbfile' should be of type pynwb.NWBFile"
#
#     assert (
#         distutils.version.LooseVersion(pynwb.__version__) >= "1.3.3"
#     ), "'write_recording' not supported for version < 1.3.3. Run pip install --upgrade pynwb"
#
#     assert save_path is None or nwbfile is None, "Either pass a save_path location, or nwbfile object, but not both!"
#
#     # Update any previous metadata with user passed dictionary
#     if hasattr(recording, "nwb_metadata"):
#         metadata = dict_deep_update(recording.nwb_metadata, metadata)
#     elif metadata is None:
#         # If not NWBRecording, make metadata from information available on Recording
#         metadata = get_nwb_metadata(recording=recording)
#
#     if nwbfile is None:
#         if Path(save_path).is_file() and not overwrite:
#             read_mode = "r+"
#         else:
#             read_mode = "w"
#
#         with pynwb.NWBHDF5IO(str(save_path), mode=read_mode) as io:
#             if read_mode == "r+":
#                 nwbfile = io.read()
#             else:
#                 # Default arguments will be over-written if contained in metadata
#                 nwbfile_kwargs = dict(
#                     session_description="Auto-generated by NwbRecordingExtractor without description.",
#                     identifier=str(uuid.uuid4()),
#                     session_start_time=datetime(1970, 1, 1),
#                 )
#                 if metadata is not None and "NWBFile" in metadata:
#                     nwbfile_kwargs.update(metadata["NWBFile"])
#                 nwbfile = pynwb.NWBFile(**nwbfile_kwargs)
#
#             add_all_to_nwbfile(
#                 recording=recording,
#                 nwbfile=nwbfile,
#                 buffer_mb=buffer_mb,
#                 metadata=metadata,
#                 use_times=use_times,
#                 write_as=write_as,
#                 es_key=es_key,
#                 write_scaled=write_scaled,
#                 compression=compression,
#                 iterate=iterate,
#             )
#
#             # Write to file
#             io.write(nwbfile)
#     else:
#         add_all_to_nwbfile(
#             recording=recording,
#             nwbfile=nwbfile,
#             buffer_mb=buffer_mb,
#             use_times=use_times,
#             metadata=metadata,
#             write_as=write_as,
#             es_key=es_key,
#             write_scaled=write_scaled,
#             compression=compression,
#             iterate=iterate,
#         )
#
#
# def get_nspikes(units_table, unit_id):
#     """Return the number of spikes for chosen unit."""
#     ids = np.array(units_table.id[:])
#     indexes = np.where(ids == unit_id)[0]
#     if not len(indexes):
#         raise ValueError(f"{unit_id} is an invalid unit_id. Valid ids: {ids}.")
#     index = indexes[0]
#     if index == 0:
#         return units_table["spike_times_index"].data[index]
#     else:
#         return units_table["spike_times_index"].data[index] - units_table["spike_times_index"].data[index - 1]
#
#
# def write_units(
#     sorting: se.SortingExtractor,
#     nwbfile,
#     property_descriptions: Optional[dict] = None,
#     skip_properties: Optional[List[str]] = None,
#     skip_features: Optional[List[str]] = None,
#     use_times: bool = True,
# ):
#     """Auxilliary function for write_sorting."""
#     unit_ids = sorting.get_unit_ids()
#     fs = sorting.get_sampling_frequency()
#     if fs is None:
#         raise ValueError("Writing a SortingExtractor to an NWBFile requires a known sampling frequency!")
#
#     all_properties = set()
#     all_features = set()
#     for unit_id in unit_ids:
#         all_properties.update(sorting.get_unit_property_names(unit_id))
#         all_features.update(sorting.get_unit_spike_feature_names(unit_id))
#
#     default_descriptions = dict(
#         isi_violation="Quality metric that measures the ISI violation ratio as a proxy for the purity of the unit.",
#         firing_rate="Number of spikes per unit of time.",
#         template="The extracellular average waveform.",
#         max_channel="The recording channel id with the largest amplitude.",
#         halfwidth="The full-width half maximum of the negative peak computed on the maximum channel.",
#         peak_to_valley="The duration between the negative and the positive peaks computed on the maximum channel.",
#         snr="The signal-to-noise ratio of the unit.",
#         quality="Quality of the unit as defined by phy (good, mua, noise).",
#         spike_amplitude="Average amplitude of peaks detected on the channel.",
#         spike_rate="Average rate of peaks detected on the channel.",
#     )
#     if property_descriptions is None:
#         property_descriptions = dict(default_descriptions)
#     else:
#         property_descriptions = dict(default_descriptions, **property_descriptions)
#     if skip_properties is None:
#         skip_properties = []
#     if skip_features is None:
#         skip_features = []
#
#     if nwbfile.units is None:
#         # Check that array properties have the same shape across units
#         property_shapes = dict()
#         for pr in all_properties:
#             shapes = []
#             for unit_id in unit_ids:
#                 if pr in sorting.get_unit_property_names(unit_id):
#                     prop_value = sorting.get_unit_property(unit_id, pr)
#                     if isinstance(prop_value, (int, np.integer, float, str, bool)):
#                         shapes.append(1)
#                     elif isinstance(prop_value, (list, np.ndarray)):
#                         if np.array(prop_value).ndim == 1:
#                             shapes.append(len(prop_value))
#                         else:
#                             shapes.append(np.array(prop_value).shape)
#                     elif isinstance(prop_value, dict):
#                         print(f"Skipping property '{pr}' because dictionaries are not supported.")
#                         skip_properties.append(pr)
#                         break
#                 else:
#                     shapes.append(np.nan)
#             property_shapes[pr] = shapes
#
#         for pr in property_shapes.keys():
#             elems = [elem for elem in property_shapes[pr] if not np.any(np.isnan(elem))]
#             if not np.all([elem == elems[0] for elem in elems]):
#                 print(f"Skipping property '{pr}' because it has variable size across units.")
#                 skip_properties.append(pr)
#
#         write_properties = set(all_properties) - set(skip_properties)
#         for pr in write_properties:
#             if pr not in property_descriptions:
#                 warnings.warn(
#                     f"Description for property {pr} not found in property_descriptions. "
#                     "Setting description to 'no description'"
#                 )
#         for pr in write_properties:
#             unit_col_args = dict(name=pr, description=property_descriptions.get(pr, "No description."))
#             if pr in ["max_channel", "max_electrode"] and nwbfile.electrodes is not None:
#                 unit_col_args.update(table=nwbfile.electrodes)
#             nwbfile.add_unit_column(**unit_col_args)
#
#         for unit_id in unit_ids:
#             unit_kwargs = dict()
#             if use_times:
#                 spkt = sorting.frame_to_time(sorting.get_unit_spike_train(unit_id=unit_id))
#             else:
#                 spkt = sorting.get_unit_spike_train(unit_id=unit_id) / sorting.get_sampling_frequency()
#             for pr in write_properties:
#                 if pr in sorting.get_unit_property_names(unit_id):
#                     prop_value = sorting.get_unit_property(unit_id, pr)
#                     unit_kwargs.update({pr: prop_value})
#                 else:  # Case of missing data for this unit and this property
#                     unit_kwargs.update({pr: np.nan})
#             nwbfile.add_unit(id=int(unit_id), spike_times=spkt, **unit_kwargs)
#
#         # TODO
#         # # Stores average and std of spike traces
#         # This will soon be updated to the current NWB standard
#         # if 'waveforms' in sorting.get_unit_spike_feature_names(unit_id=id):
#         #     wf = sorting.get_unit_spike_features(unit_id=id,
#         #                                          feature_name='waveforms')
#         #     relevant_ch = most_relevant_ch(wf)
#         #     # Spike traces on the most relevant channel
#         #     traces = wf[:, relevant_ch, :]
#         #     traces_avg = np.mean(traces, axis=0)
#         #     traces_std = np.std(traces, axis=0)
#         #     nwbfile.add_unit(
#         #         id=id,
#         #         spike_times=spkt,
#         #         waveform_mean=traces_avg,
#         #         waveform_sd=traces_std
#         #     )
#
#         # Check that multidimensional features have the same shape across units
#         feature_shapes = dict()
#         for ft in all_features:
#             shapes = []
#             for unit_id in unit_ids:
#                 if ft in sorting.get_unit_spike_feature_names(unit_id):
#                     feat_value = sorting.get_unit_spike_features(unit_id, ft)
#                     if isinstance(feat_value[0], (int, np.integer, float, str, bool)):
#                         break
#                     elif isinstance(feat_value[0], (list, np.ndarray)):  # multidimensional features
#                         if np.array(feat_value).ndim > 1:
#                             shapes.append(np.array(feat_value).shape)
#                             feature_shapes[ft] = shapes
#                     elif isinstance(feat_value[0], dict):
#                         print(f"Skipping feature '{ft}' because dictionaries are not supported.")
#                         skip_features.append(ft)
#                         break
#                 else:
#                     print(f"Skipping feature '{ft}' because not share across all units.")
#                     skip_features.append(ft)
#                     break
#
#         nspikes = {k: get_nspikes(nwbfile.units, int(k)) for k in unit_ids}
#
#         for ft in feature_shapes.keys():
#             # skip first dimension (num_spikes) when comparing feature shape
#             if not np.all([elem[1:] == feature_shapes[ft][0][1:] for elem in feature_shapes[ft]]):
#                 print(f"Skipping feature '{ft}' because it has variable size across units.")
#                 skip_features.append(ft)
#
#         for ft in set(all_features) - set(skip_features):
#             values = []
#             if not ft.endswith("_idxs"):
#                 for unit_id in sorting.get_unit_ids():
#                     feat_vals = sorting.get_unit_spike_features(unit_id, ft)
#
#                     if len(feat_vals) < nspikes[unit_id]:
#                         skip_features.append(ft)
#                         print(f"Skipping feature '{ft}' because it is not defined for all spikes.")
#                         break
#                         # this means features are available for a subset of spikes
#                         # all_feat_vals = np.array([np.nan] * nspikes[unit_id])
#                         # feature_idxs = sorting.get_unit_spike_features(unit_id, feat_name + '_idxs')
#                         # all_feat_vals[feature_idxs] = feat_vals
#                     else:
#                         all_feat_vals = feat_vals
#                     values.append(all_feat_vals)
#
#                 flatten_vals = [item for sublist in values for item in sublist]
#                 nspks_list = [sp for sp in nspikes.values()]
#                 spikes_index = np.cumsum(nspks_list).astype("int64")
#                 if ft in nwbfile.units:  # If property already exists, skip it
#                     warnings.warn(f"Feature {ft} already present in units table, skipping it")
#                     continue
#                 set_dynamic_table_property(
#                     dynamic_table=nwbfile.units,
#                     row_ids=[int(k) for k in unit_ids],
#                     property_name=ft,
#                     values=flatten_vals,
#                     index=spikes_index,
#                 )
#     else:
#         warnings.warn("The nwbfile already contains units. These units will not be over-written.")
#
#
# def write_sorting(
#     sorting: se.SortingExtractor,
#     save_path: PathType = None,
#     overwrite: bool = False,
#     nwbfile=None,
#     property_descriptions: Optional[dict] = None,
#     skip_properties: Optional[List[str]] = None,
#     skip_features: Optional[List[str]] = None,
#     use_times: bool = True,
#     metadata: dict = None,
# ):
#     """
#     Primary method for writing a SortingExtractor object to an NWBFile.
#
#     Parameters
#     ----------
#     sorting: SortingExtractor
#     save_path: PathType
#         Required if an nwbfile is not passed. The location where the NWBFile either exists, or will be written.
#     overwrite: bool
#         If using save_path, whether or not to overwrite the NWBFile if it already exists.
#     nwbfile: NWBFile
#         Required if a save_path is not specified. If passed, this function
#         will fill the relevant fields within the nwbfile. E.g., calling
#         spikeextractors.NwbRecordingExtractor.write_recording(
#             my_recording_extractor, my_nwbfile
#         )
#         will result in the appropriate changes to the my_nwbfile object.
#     property_descriptions: dict
#         For each key in this dictionary which matches the name of a unit
#         property in sorting, adds the value as a description to that
#         custom unit column.
#     skip_properties: list of str
#         Each string in this list that matches a unit property will not be written to the NWBFile.
#     skip_features: list of str
#         Each string in this list that matches a spike feature will not be written to the NWBFile.
#     use_times: bool (optional, defaults to False)
#         If True, the times are saved to the nwb file using sorting.frame_to_time(). If False (default),
#         the sampling rate is used.
#     metadata: dict
#         Information for constructing the nwb file (optional).
#         Only used if no nwbfile exists at the save_path, and no nwbfile was directly passed.
#     """
#     assert save_path is None or nwbfile is None, "Either pass a save_path location, or nwbfile object, but not both!"
#     if nwbfile is not None:
#         assert isinstance(nwbfile, pynwb.NWBFile), "'nwbfile' should be a pynwb.NWBFile object!"
#
#     if nwbfile is None:
#         if Path(save_path).is_file() and not overwrite:
#             read_mode = "r+"
#         else:
#             read_mode = "w"
#
#         with pynwb.NWBHDF5IO(str(save_path), mode=read_mode) as io:
#             if read_mode == "r+":
#                 nwbfile = io.read()
#             else:
#                 nwbfile_kwargs = dict(
#                     session_description="Auto-generated by NwbSortingExtractor without description.",
#                     identifier=str(uuid.uuid4()),
#                     session_start_time=datetime(1970, 1, 1),
#                 )
#                 if metadata is not None and "NWBFile" in metadata:
#                     nwbfile_kwargs.update(metadata["NWBFile"])
#                 nwbfile = pynwb.NWBFile(**nwbfile_kwargs)
#             write_units(
#                 sorting=sorting,
#                 nwbfile=nwbfile,
#                 property_descriptions=property_descriptions,
#                 skip_properties=skip_properties,
#                 skip_features=skip_features,
#                 use_times=use_times,
#             )
#             io.write(nwbfile)
#     else:
#         write_units(
#             sorting=sorting,
#             nwbfile=nwbfile,
#             property_descriptions=property_descriptions,
#             skip_properties=skip_properties,
#             skip_features=skip_features,
#             use_times=use_times,
#         )
