"""Authors: Cody Baker and Ben Dichter."""
import spikeextractors as se
from pynwb import NWBFile

from .baserecordingextractorinterface import BaseRecordingExtractorInterface


class BaseLFPExtractorInterface(BaseRecordingExtractorInterface):
    """Primary class for all LFP data interfaces."""

    def get_metadata(self):
        """Auto-fill as much of the metadata as possible. Must comply with metadata schema."""
        metadata = super().get_metadata()
        metadata['Ecephys'].pop('ElectricalSeries')
        metadata['Ecephys'].update(
            LFPElectricalSeries=dict(),
        )
        return metadata

    def run_conversion(self, nwbfile: NWBFile, metadata: dict = None, stub_test: bool = False):
        """
        Primary function for converting LFP extractor data to nwb.

        Parameters
        ----------
        nwbfile: pynwb.NWBFile
        metadata: dict
        stub_test: bool, optional (default False)
            If True, will truncate the data to run the conversion faster and take up less memory.
        write_ecephys_metadata: bool, optional (default False)
            If True, will use the information in metadata['Ecephys'] to write electrode metadata into the NWBFile.
        """
        recording_extractor = self.subset_recording(stub_test=stub_test)
        se.NwbRecordingExtractor.write_recording(
            recording_extractor,
            nwbfile=nwbfile,
            metadata=metadata,
            write_as_lfp=True
        )