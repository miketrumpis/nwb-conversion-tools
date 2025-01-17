"""Authors: Cody Baker."""
from pathlib import Path
from natsort import natsorted

from spikeextractors import MultiRecordingChannelExtractor, NeuralynxRecordingExtractor

from ..baserecordingextractorinterface import BaseRecordingExtractorInterface
from ....utils.json_schema import FolderPathType
from ....utils import map_si_object_to_writer


class NeuralynxRecordingInterface(BaseRecordingExtractorInterface):
    """Primary data interface class for converting the Neuralynx format."""

    RX = MultiRecordingChannelExtractor

    def __init__(self, folder_path: FolderPathType):
        self.subset_channels = None
        self.source_data = dict(folder_path=folder_path)
        neuralynx_files = natsorted([str(x) for x in Path(folder_path).iterdir() if ".ncs" in x.suffixes])
        extractors = [NeuralynxRecordingExtractor(filename=filename, seg_index=0) for filename in neuralynx_files]
        gains = [extractor.get_channel_gains()[0] for extractor in extractors]
        for extractor in extractors:
            extractor.clear_channel_gains()
        self.recording_extractor = self.RX(extractors)
        self.recording_extractor.set_channel_gains(gains=gains)
        self.writer_class = map_si_object_to_writer(self.recording_extractor)(self.recording_extractor)
