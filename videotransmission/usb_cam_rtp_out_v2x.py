#!/usr/bin/env python3
import _thread
import argparse
import sys

import numpy as np

sys.path.append('../')

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')

from gi.repository import GLib, Gst, GstRtspServer
from deepstreamapps.common.is_aarch_64 import is_aarch64
from deepstreamapps.common.bus_call import bus_call
import pyds
import threading
from udpsocket import *
from tlvmessage import *
import socket
import time
import traceback

from tlvmessage import *

PGIE_CLASS_ID_VEHICLE = 0
PGIE_CLASS_ID_BICYCLE = 1
PGIE_CLASS_ID_PERSON = 2
PGIE_CLASS_ID_ROADSIGN = 3


def osd_sink_pad_buffer_probe(pad, info, u_data):
    frame_number = 0
    # Intiallizing object counter with 0.
    obj_counter = {
        PGIE_CLASS_ID_VEHICLE: 0,
        PGIE_CLASS_ID_PERSON: 0,
        PGIE_CLASS_ID_BICYCLE: 0,
        PGIE_CLASS_ID_ROADSIGN: 0
    }
    num_rects = 0

    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    # Retrieve batch metadata from the gst_buffer
    # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
    # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            # Note that l_frame.data needs a cast to pyds.NvDsFrameMeta
            # The casting is done by pyds.NvDsFrameMeta.cast()
            # The casting also keeps ownership of the underlying memory
            # in the C code, so the Python garbage collector will leave
            # it alone.
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        frame_number = frame_meta.frame_num
        num_rects = frame_meta.num_obj_meta
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            try:
                # Casting l_obj.data to pyds.NvDsObjectMeta
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break
            obj_counter[obj_meta.class_id] += 1
            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        # Acquiring a display meta object. The memory ownership remains in
        # the C code so downstream plugins can still access it. Otherwise
        # the garbage collector will claim it when this probe function exits.
        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        display_meta.num_labels = 1
        py_nvosd_text_params = display_meta.text_params[0]
        # Setting display text to be shown on screen
        # Note that the pyds module allocates a buffer for the string, and the
        # memory will not be claimed by the garbage collector.
        # Reading the display_text field here will return the C address of the
        # allocated string. Use pyds.get_string() to get the string content.

        # py_nvosd_text_params.display_text = "Frame Number={} Number of Objects={} " \
        #                                     "Vehicle_count={} Person_count={}".format(frame_number, num_rects,
        #                                                                               obj_counter[
        #                                                                                   PGIE_CLASS_ID_VEHICLE],
        #                                                                               obj_counter[PGIE_CLASS_ID_PERSON])

        # Now set the offsets where the string should appear
        py_nvosd_text_params.x_offset = 10
        py_nvosd_text_params.y_offset = 12

        # Font , font-color and font-size
        py_nvosd_text_params.font_params.font_name = "Serif"
        py_nvosd_text_params.font_params.font_size = 10
        # set(red, green, blue, alpha); set to White
        py_nvosd_text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)

        # Text background color
        py_nvosd_text_params.set_bg_clr = 1
        # set(red, green, blue, alpha); set to Black
        py_nvosd_text_params.text_bg_clr.set(0.0, 0.0, 0.0, 1.0)
        # Using pyds.get_string() to get display_text as string
        print(pyds.get_string(py_nvosd_text_params.display_text))
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    return Gst.PadProbeReturn.OK


def main(args):
    # Standard GStreamer initialization
    Gst.init(None)

    # Create gstreamer elements
    # Create Pipeline element that will form a connection of other elements
    print("Creating Pipeline \n ")
    pipeline = Gst.Pipeline()
    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")

    # Source element for reading from the file
    print("Creating Source \n ")
    source = Gst.ElementFactory.make("v4l2src", "usb-cam-source")
    if not source:
        sys.stderr.write(" Unable to create Source \n")

    caps_v4l2src = Gst.ElementFactory.make("capsfilter", "v4l2src_caps")
    if not caps_v4l2src:
        sys.stderr.write(" Unable to create v4l2src capsfilter \n")

    print("Creating Video Converter \n")

    # Adding videoconvert -> nvvideoconvert as not all
    # raw formats are supported by nvvideoconvert;
    # Say YUYV is unsupported - which is the common
    # raw format for many logi usb cams
    # In case we have a camera with raw format supported in
    # nvvideoconvert, GStreamer plugins' capability negotiation
    # shall be intelligent enough to reduce compute by
    # videoconvert doing passthrough (TODO we need to confirm this)

    # videoconvert to make sure a superset of raw formats are supported
    vidconvsrc = Gst.ElementFactory.make("videoconvert", "convertor_src1")
    if not vidconvsrc:
        sys.stderr.write(" Unable to create videoconvert \n")

    # nvvideoconvert to convert incoming raw buffers to NVMM Mem (NvBufSurface API)
    nvvidconvsrc = Gst.ElementFactory.make("nvvideoconvert", "convertor_src2")
    if not nvvidconvsrc:
        sys.stderr.write(" Unable to create Nvvideoconvert \n")

    caps_vidconvsrc = Gst.ElementFactory.make("capsfilter", "nvmm_caps")
    if not caps_vidconvsrc:
        sys.stderr.write(" Unable to create capsfilter \n")

    ##############################################################################################

    # Create nvstreammux instance to form batches from one or more sources.
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")

    # Use nvinfer to run inferencing on decoder's output,
    # behaviour of inferencing is set through config file
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")

    # Use convertor to convert from NV12 to RGBA as required by nvosd
    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
    if not nvvidconv:
        sys.stderr.write(" Unable to create nvvidconv \n")

    # Create OSD to draw on the converted RGBA buffer
    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    if not nvosd:
        sys.stderr.write(" Unable to create nvosd \n")
    nvvidconv_postosd = Gst.ElementFactory.make("nvvideoconvert", "convertor_postosd")
    if not nvvidconv_postosd:
        sys.stderr.write(" Unable to create nvvidconv_postosd \n")

    # Create a caps filter
    caps = Gst.ElementFactory.make("capsfilter", "filter")
    caps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM), format=I420"))

    # Make the encoder
    encoder = None
    if codec == "H264":
        encoder = Gst.ElementFactory.make("nvv4l2h264enc", "encoder")
        print("Creating H264 Encoder")
    elif codec == "H265":
        encoder = Gst.ElementFactory.make("nvv4l2h265enc", "encoder")
        print("Creating H265 Encoder")
    if not encoder:
        sys.stderr.write(" Unable to create encoder")
    encoder.set_property('bitrate', bitrate)
    if is_aarch64():
        encoder.set_property('preset-level', 1)
        encoder.set_property('insert-sps-pps', 1)

    # Make the payload-encode video into RTP packets
    rtppay = None
    if codec == "H264":
        rtppay = Gst.ElementFactory.make("rtph264pay", "rtppay")
        print("Creating H264 rtppay")
    elif codec == "H265":
        rtppay = Gst.ElementFactory.make("rtph265pay", "rtppay")
        print("Creating H265 rtppay")
    if not rtppay:
        sys.stderr.write(" Unable to create rtppay")

    # Make the UDP sink
    sink = Gst.ElementFactory.make("udpsink", "udpsink")
    if not sink:
        sys.stderr.write(" Unable to create udpsink")

    sink.set_property('host', udpsink_host)
    sink.set_property('port', udpsink_port)
    sink.set_property('async', False)
    sink.set_property('sync', 1)

    print("Playing camera %s " % device_cam)
    caps_v4l2src.set_property('caps', Gst.Caps.from_string("video/x-raw, framerate=25/1"))
    caps_vidconvsrc.set_property('caps', Gst.Caps.from_string("video/x-raw(memory:NVMM)"))

    source.set_property('device', device_cam)
    # source.set_property('caps', Gst.Caps.from_string("video/x-raw, width=640, height=480, framerate=30/1"))

    streammux.set_property('width', 640)
    streammux.set_property('height', 480)
    streammux.set_property('batch-size', 1)
    streammux.set_property('batched-push-timeout', 4000000)

    pgie.set_property('config-file-path', "dstest1_pgie_config.txt")

    print("Adding elements to Pipeline \n")
    pipeline.add(source)

    pipeline.add(caps_v4l2src)
    pipeline.add(vidconvsrc)
    pipeline.add(nvvidconvsrc)
    pipeline.add(caps_vidconvsrc)

    pipeline.add(streammux)
    pipeline.add(pgie)
    pipeline.add(nvvidconv)
    pipeline.add(nvosd)
    pipeline.add(nvvidconv_postosd)
    pipeline.add(caps)
    pipeline.add(encoder)
    pipeline.add(rtppay)
    pipeline.add(sink)

    print("Linking elements in the Pipeline \n")
    source.link(caps_v4l2src)
    caps_v4l2src.link(vidconvsrc)
    vidconvsrc.link(nvvidconvsrc)
    nvvidconvsrc.link(caps_vidconvsrc)

    sinkpad = streammux.get_request_pad("sink_0")
    if not sinkpad:
        sys.stderr.write(" Unable to get the sink pad of streammux \n")

    srcpad = caps_vidconvsrc.get_static_pad("src")
    if not srcpad:
        sys.stderr.write(" Unable to get source pad of caps_vidconvsrc \n")

    srcpad.link(sinkpad)
    streammux.link(pgie)
    pgie.link(nvvidconv)
    nvvidconv.link(nvosd)
    nvosd.link(nvvidconv_postosd)
    nvvidconv_postosd.link(caps)
    caps.link(encoder)
    encoder.link(rtppay)
    rtppay.link(sink)

    # create an event loop and feed gstreamer bus mesages to it
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    osdsinkpad = nvosd.get_static_pad("sink")
    if not osdsinkpad:
        sys.stderr.write(" Unable to get sink pad of nvosd \n")

    osdsinkpad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe, 0)

    # start play back and listen to events
    print("Starting pipeline \n")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except:
        pass
    # cleanup
    pipeline.set_state(Gst.State.NULL)


def loop_to_v2x(tlv_enable=False):
    """
    将RTP包打包到一个包中，按周期发送到OBU。
    +++++++++++++++++++++++++++++++++++++++++++++++++++++++
    | 8bit | 8bit | 16bit | 16bit |...| 16bit |  packet ...
    +++++++++++++++++++++++++++++++++++++++++++++++++++++++
    第一个byte填写类型，第二个byte填写有几个包，后续的2byte填写对应的包的长度，新包在前旧包在后。
    :return:
    """
    tlv_en = tlv_enable
    packet_type = 4
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # sock.settimeout(10)
    sock.bind(('192.168.62.223', 30300))

    remote_address = ('192.168.62.199', 30299)
    buffer_len = 128  # 最大缓存长度
    send_msg_list = []
    send_msg_lengths = np.zeros(buffer_len)
    index = 0
    send_time = 0
    while True:
        msg = sock.recv(2048)
        if msg == b'':
            raise RuntimeError("Socket connection broken!")
        if len(msg) > PACKET_MAX_LENGTH:
            pass
        elif index < buffer_len:
            send_msg_list.append(msg)
            send_msg_lengths[index] = len(msg)
            index += 1
        else:
            send_msg_list.pop(0)  # 丢弃最早的包
            send_msg_list.append(msg)
            np.roll(send_msg_lengths, -1)  # 循环左移一位
            send_msg_lengths[buffer_len - 1] = len(msg)
        now_time = time.time() * 1000  # ms
        if now_time - send_time >= SEND_PERIOD and index != 0:
            packets = b''
            packets_len = 0
            packet_num = 0
            message = b''
            for i in range(index - 1, -1, -1):
                packets = packets + send_msg_list[i]
                packets_len += send_msg_lengths[i]
                packet_num += 1
                if i == 0 or (packets_len + send_msg_lengths[i - 1]) > PACKET_MAX_LENGTH:
                    break
            message = packet_type.to_bytes(1, 'big') + packet_num.to_bytes(1, 'big')
            for j in range(packet_num):
                length = send_msg_lengths[index - 1 - j]
                message = message + int(length).to_bytes(2, 'big')
            message = message + packets
            print('message length:{},packet_num:{}\n'.format(len(message), packet_num))
            print(send_msg_lengths)
            try:
                if tlv_en:
                    tlv_msg = TLVMessage(message, SEND,
                                         new_msg=True,
                                         config=(b'\x00\x00\x00\x70',   # aid
                                                 b'\x00\x00\x00\x0b',   # traffic_period
                                                 b'\x00\x00\x00\x7f',   # priority
                                                 b'\x00\x00\xff\xff'))  # traffic_id
                    send_len = sock.sendto(tlv_msg.get_tlv_raw_message(), remote_address)
                    # print('tlv_msg_len:{},message len:{}'.format(len(tlv_msg.get_tlv_raw_message()), len(message)))
                else:
                    send_len = sock.sendto(message, remote_address)
                    # print('send_len:{},message len:{}'.format(send_len, len(message)))
            except Exception as _:
                traceback.print_exc()
                sys.exit(0)
            if send_len == 0:
                raise RuntimeError("Socket connection broken!")
            send_time = time.time() * 1000  # ms
            # 清空缓存
            index = 0
            send_msg_list = []
            send_msg_lengths[:] = 0


PACKET_MAX_LENGTH = 1450  # byte
SEND_PERIOD = 80  # ms
udpsink_host = '192.168.62.223'
udpsink_port = 30300
codec = 'H265'
bitrate = 30000
stream_path = '/opt/nvidia/deepstream/deepstream-6.1/samples/streams/sample_720p.h264'
device_cam = '/dev/video0'

if __name__ == '__main__':
    try:
        _thread.start_new_thread(loop_to_v2x, (False,))
    except _thread.error:
        print("Unable to start thread: loop_to_v2x.")
    sys.exit(main(sys.argv))
