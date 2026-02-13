#!/usr/bin/env python3

"""
OLED Emulator with Fixed Header and Scrolling Text Lines
256x64 display simulation with luma.emulator
Continuous upward scrolling sequence with data from object
Line1 is fixed header, Lines 2-10 scroll
Each line shows: Index | Line | Destination | Time (right justified)
"""
from typing import Any

from luma.emulator.device import pygame
from luma.core.render import canvas
from PIL import Image, ImageDraw, ImageFont
import RPi.GPIO as GPIO

from luma.core.interface.serial import spi, noop
from luma.oled.device import ssd1322


import time
import sys

import json
from datetime import datetime
from urllib.request import urlopen, Request

STATION_ID = "490008987N"
STATION_ID2 = "490008613S"
#STATION_ID = "490011842R"  # Hanover Park

API_ID = "2cbe9909205f4f62a92266395775cf5b"

class ScrollingTextLines:
    """Handles the creation and scrolling of text lines with specific sequence"""

    MAX_PAGES = 3
    MAX_SCROLLABLE_LINES = MAX_PAGES * 3
    MAX_TOTAL_LINES = 1 + MAX_SCROLLABLE_LINES + 3  # header + scrollable + duplicate

    def __init__(self, width=256, height=144):
        self.bitmap_width = width
        self.line_height = 16
        self.display_height = 48  # 3 lines visible
        self.line_data = []
        self.pending_line_data = None
        self.no_departures = False
        self.bitmap = None
        self.extended_bitmap = None
        self.extended_height = 0
        self.scroll_offset = 0  # pixel offset into extended bitmap
        self.target_offsets = []  # pixel offsets for each page
        self.last_update_time = time.time()
        self.last_state_change_time = time.time()
        self.is_scrolling = False
        self.scroll_start_offset = 0
        self.scroll_start_time = 0
        self.num_scrollable_lines = 0
        self.pause_time = 5.0
        self.scroll_time = 1.0
        self.at_wrap = False  # Track if we're at the wrap (duplicate page)
        # Allocate the double buffer once at max size
        self._allocate_double_buffer()
        self.set_line_data([])

    def _allocate_double_buffer(self):
        # Allocate the extended bitmap once at max size
        self.extended_bitmap = Image.new('1', (self.bitmap_width, self.MAX_TOTAL_LINES * self.line_height), 0)
        self.extended_height = self.extended_bitmap.height

    def set_line_data(self, new_data):
        """Defer new data until scrolling returns to the start (page 1) or apply immediately if not scrolling."""
        # If no scrolling or at the start (page 1), update immediately
        scrolling_active = (not self.no_departures and len(self.line_data) > 4)
        at_start = (self.scroll_offset == 0 and not self.is_scrolling and not self.at_wrap)
        if scrolling_active and not at_start:
            self.pending_line_data = new_data
            return

        # Otherwise, update immediately
        if new_data is None or len(new_data) == 0:
            self.no_departures = True
            self.line_data = []  # No header, no lines
        else:
            self.no_departures = False
            self.line_data = new_data[:10]  # Max 10 lines (including header)

        self._rebuild_bitmaps()
        self.scroll_offset = 0  # Always reset scroll to top after new data
        self.is_scrolling = False
        self.at_wrap = False
        self.pending_line_data = None

    def _rebuild_bitmaps(self):
        if self.no_departures:
            self.bitmap = None
            if self.extended_bitmap is not None:
                self.extended_bitmap.paste(0, (0, 0, self.bitmap_width, self.extended_height))
            self.target_offsets = [0]
            self.scroll_offset = 0
            self.num_scrollable_lines = 0
            return
        # Always include header as first row
        total_lines = len(self.line_data)
        if total_lines == 0:
            self.bitmap = None
            if self.extended_bitmap is not None:
                self.extended_bitmap.paste(0, (0, 0, self.bitmap_width, self.extended_height))
            self.target_offsets = [0]
            self.scroll_offset = 0
            self.num_scrollable_lines = 0
            return
        # Draw into the preallocated extended_bitmap
        self.extended_bitmap.paste(0, (0, 0, self.bitmap_width, self.extended_height))
        # Draw header and scrollable lines
        try:
            font = ImageFont.truetype("DejaVuSansMono.ttf", 10)
        except:
            font = ImageFont.load_default()
        draw = ImageDraw.Draw(self.extended_bitmap)
        for i in range(total_lines):
            y_pos = i * self.line_height
            draw.rectangle([0, y_pos, self.bitmap_width - 1, y_pos + self.line_height - 1], outline=None, fill="black")
            self.draw_line_row(draw, y_pos, self.line_data[i], font, self.bitmap_width, i+1)
        # If scrolling, add duplicate of first 3 scrollable lines after header
        if total_lines > 4:
            n = total_lines - 1
            self.num_scrollable_lines = n
            self.num_groups = (n + 2) // 3
            self.padded_lines = self.num_groups * 3
            # Copy first 3 scrollable lines (not header) to the end
            src_y = self.line_height
            dst_y = (1 + self.padded_lines) * self.line_height
            region = self.extended_bitmap.crop((0, src_y, self.bitmap_width, src_y + 3 * self.line_height))
            self.extended_bitmap.paste(region, (0, dst_y))
            self.target_offsets = [i * 3 * self.line_height for i in range(self.num_groups + 1)]
        else:
            self.num_scrollable_lines = max(3, total_lines - 1)
            self.num_groups = 1
            self.padded_lines = self.num_scrollable_lines
            self.target_offsets = [0]
        self.bitmap = self.extended_bitmap.crop((0, 0, self.bitmap_width, (1 + self.padded_lines) * self.line_height))
        self.extended_height = self.extended_bitmap.height
        self.scroll_offset = 0

    def draw_line_row_main(self, draw, y_pos, line_data, font, width, index):
        x_pos = 2
        if line_data:
            # Accept ArrivalData or tuple
            line_text = line_data.LineName
            destination = line_data.Destination
            index_text = str(index)
            draw.text((x_pos, y_pos + 3), index_text, font=font, fill="white")
            x_pos += 20
            draw.text((x_pos, y_pos + 3), line_text, font=font, fill="white")
            x_pos += 30
            draw.text((x_pos, y_pos + 3), destination, font=font, fill="white")

    def draw_line_row_time(self, draw, y_pos, line_data, font, width):
        if line_data:
            time_str = line_data.DisplayTime
            time_width = draw.textlength(time_str, font=font)
            time_x = width - time_width - 2
            draw.text((time_x, y_pos + 3), time_str, font=font, fill="white")

    def draw_line_row(self, draw, y_pos, line_data, font, width, index):
        self.draw_line_row_main(draw, y_pos, line_data, font, width, index)
        self.draw_line_row_time(draw, y_pos, line_data, font, width)

    def create_text_lines_bitmap(self, all_lines, pad_to=None):
        if self.no_departures or len(all_lines) == 0:
            return None
        total_lines = pad_to if pad_to is not None else len(all_lines)
        img = Image.new('1', (self.bitmap_width, total_lines * self.line_height), 0)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSansMono.ttf", 10)
        except:
            font = ImageFont.load_default()
        for i in range(total_lines):
            y_pos = i * self.line_height
            draw.rectangle([0, y_pos, self.bitmap_width - 1, y_pos + self.line_height - 1], outline=None, fill="black")
            if i < len(all_lines):
                self.draw_line_row(draw, y_pos, all_lines[i], font, self.bitmap_width, i+1)
        return img

    def create_wrapped_bitmap(self):
        n = self.padded_lines
        if n <= 3:
            return self.bitmap
        ext_height = (n+1) * self.line_height + 3 * self.line_height
        ext_img = Image.new('1', (self.bitmap_width, ext_height), 0)
        ext_img.paste(self.bitmap, (0, 0))
        if self.bitmap:
            # Copy the first 3 scrollable lines (not header) to the end for seamless scroll
            wrap = self.bitmap.crop((0, self.line_height, self.bitmap_width, self.line_height + 3 * self.line_height))
            ext_img.paste(wrap, (0, (n+1) * self.line_height))
        return ext_img

    def update(self):
        current_time = time.time()
        self.last_update_time = current_time
        if self.no_departures or self.num_scrollable_lines <= 3:
            # If not scrolling, apply pending data immediately
            if self.pending_line_data is not None:
                self.set_line_data(self.pending_line_data)
                return

        # Check if the timestamps have changed to trigger data update
        dirty = False
        for line in self.line_data:
            updated_display_time = line.GetDisplayTime()
            if line.DisplayTime != updated_display_time:
                line.DisplayTime = updated_display_time
                dirty = True
        if dirty:
            pending_line_data = self.line_data
            self.set_line_data(pending_line_data)
            return

        state_elapsed = current_time - self.last_state_change_time
        page_height = 3 * self.line_height
        num_real_pages = len(self.target_offsets) - 1
        max_offset = self.target_offsets[-1]
        if not self.is_scrolling:
            # If we're at the wrap, pause, then reset to top of page 1 and immediately start scrolling to page 2
            if self.at_wrap:
                if state_elapsed >= self.pause_time:
                    self.scroll_offset = 0  # Reset to top of page 1
                    self.at_wrap = False
                    self.last_state_change_time = current_time
                    # If pending data, apply it now
                    if self.pending_line_data is not None:
                        self.set_line_data(self.pending_line_data)
                        return
                    # Immediately start scrolling to page 2
                    self.is_scrolling = True
                    self.scroll_start_time = current_time
                    self.scroll_start_offset = self.scroll_offset
                return
            # Pause at each page
            if state_elapsed >= self.pause_time:
                self.is_scrolling = True
                self.scroll_start_time = current_time
                self.scroll_start_offset = self.scroll_offset
                self.last_state_change_time = current_time
        else:
            scroll_elapsed = current_time - self.scroll_start_time
            scroll_progress = min(scroll_elapsed / self.scroll_time, 1.0)
            # Next page offset
            next_offset = self.scroll_start_offset + page_height
            if next_offset > max_offset:
                next_offset = max_offset
            distance = next_offset - self.scroll_start_offset
            self.scroll_offset = self.scroll_start_offset + int(distance * scroll_progress)
            if scroll_progress >= 1.0:
                self.is_scrolling = False
                self.last_state_change_time = current_time
                self.scroll_offset = next_offset
                # If we've just scrolled to the duplicate page, set at_wrap and pause
                if self.scroll_offset >= max_offset:
                    self.at_wrap = True
                    self.last_state_change_time = current_time
                    return  # End update here to avoid double-scrolling
                # Do not apply pending_line_data here; only at start

    def start_scrolling(self):
        self.is_scrolling = True
        self.scroll_start_time = time.time()
        self.scroll_start_offset = self.scroll_offset
        self.last_state_change_time = time.time()

    def render_no_departures_image(self):
        """Render 'No Departures Planned' centered in the display area."""
        img = Image.new('1', (self.bitmap_width, self.display_height), 0)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSansMono.ttf", 16)
        except:
            font = ImageFont.load_default()
        text = "No Scheduled Departures"
        # Use getbbox for accurate text size (Pillow >=7.0.0), fallback to getsize
        try:
            bbox = font.getbbox(text)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            w, h = font.getsize(text)
        x = (self.bitmap_width - w) // 2
        y = (self.display_height - h) // 2
        draw.text((x, y), text, font=font, fill="white")
        return img

    def get_visible_portion(self):
        if self.no_departures or self.bitmap is None:
            return (self.render_no_departures_image(), None)
        header_img = self.extended_bitmap.crop((0, 0, self.bitmap_width, self.line_height))
        if self.num_scrollable_lines <= 3:
            scroll_img = self.extended_bitmap.crop((0, self.line_height, self.bitmap_width, self.line_height + self.display_height))
            return (header_img, scroll_img)
        crop_area = (0, int(self.scroll_offset) + self.line_height, self.bitmap_width, int(self.scroll_offset) + self.line_height + self.display_height)
        scroll_img = self.extended_bitmap.crop(crop_area)
        return (header_img, scroll_img)

    def get_current_lines(self):
        if self.no_departures or self.num_scrollable_lines == 0:
            return "No Departures"
        if self.num_scrollable_lines <= 3:
            return f"Line2-{len(self.line_data)}"
        pos_in_range = int(self.scroll_offset) // self.line_height
        start_line_num = pos_in_range + 2
        end_line_num = min(start_line_num + 2, len(self.line_data))
        return f"Line{start_line_num}-{end_line_num}"

    def get_state_description(self):
        if self.no_departures:
            return "No Departures"
        if self.num_scrollable_lines <= 3:
            return "Static"
        # Use scroll_offset to determine the current group
        if not self.target_offsets:
            return "Scroll group 1"
        group = 1
        for i, offset in enumerate(self.target_offsets):
            if self.scroll_offset < offset:
                group = i
                break
        else:
            group = len(self.target_offsets)
        return f"Scroll group {group}"

    def get_current_data(self):
        if self.no_departures or self.num_scrollable_lines == 0:
            return [(1, "No Departures Planned", "", "")]
        start_line = int(self.scroll_offset // self.line_height) + 1
        lines_data = []
        for i in range(3):
            line_num = start_line + i
            if line_num < len(self.line_data):
                ld = self.line_data[line_num]
                lines_data.append((line_num+1, ld.LineName, ld.Destination, ld.DisplayTime))
            else:
                lines_data.append((line_num+1, "", "", ""))
        return lines_data



# Used to get live data from the TfL API and represent a specific services and it's details.
class LiveTime(object):
    # The last time an API call was made to get new data.
    LastUpdate = datetime.now()

    # * Change this method to implement your own API *
    def __init__(self, Data):
        self.Destination = str(Data['destinationName'])
        self.ExptArrival = self.convertUTCtoLocal(str(Data['expectedArrival']))
        self.DisplayTime = self.GetDisplayTime()
        self.ID = str(Data['id'])
        self.LineName = str(Data['lineName'])
        self.Via = "This is a %s line train, to %s" % (str(Data['lineName']), str(
            Data['destinationName'] if 'destinationName' in Data else str(Data['towards'])))

    # The API gives time formats in UTC format, but during BST all times are one hour out. This corrects the issue.
    def convertUTCtoLocal(self, dateTimeInput):
        datetimeTemp = datetime.strptime(dateTimeInput, '%Y-%m-%dT%H:%M:%SZ')
        datetimeTemp = datetimeTemp + (datetime.now() - datetime.utcnow())
        return datetimeTemp.strftime('%Y-%m-%dT%H:%M:%S')

    # Returns the value to display the time on the board.
    def GetDisplayTime(self):
        # Last time the display screen was updated to reflect the new time of arrival.
        self.LastStaticUpdate = datetime.now()
        if self.TimeInMin() <= 1:
            return ' Due'
        elif self.TimeInMin() >= 15:
            return ' ' + datetime.strptime(self.ExptArrival, '%Y-%m-%dT%H:%M:%S').strftime(
                "%H:%M" )
        else:
            return ' %dmin' % self.TimeInMin()

    def TimeInMin(self):
        return (datetime.strptime(self.ExptArrival, '%Y-%m-%dT%H:%M:%S') - datetime.now()).total_seconds() / 60

    # Returns true or false dependent upon if the last time an API data call was made was over the request limit; to prevent spamming the API feed.
    @staticmethod
    def TimePassed():
        return (datetime.now() - LiveTime.LastUpdate).total_seconds() > 15

    # Return true or false dependent upon if the last time the display was updated was over the static update limit. This prevents updating the display to frequently to increase performance.
    def TimePassedStatic(self):
        return ("min" in self.DisplayTime) and (
                    datetime.now() - self.LastStaticUpdate).total_seconds() > 15

    # Calls the API and gets the data from it, returning a list of LiveTime objects to be used in the program.
    # * Change this method to implement your own API *
    @staticmethod
    def GetData(station_id=STATION_ID):
        LiveTime.LastUpdate = datetime.now()
        services = []

        url = "https://api.tfl.gov.uk/StopPoint/%s/Arrivals?app_id=%s&app_key=%s" % (station_id, API_ID,
                                                                                     API_ID)
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(req) as conn:
                tempServices = json.loads(conn.read())
                for service in tempServices:
                    # If not in excluded services list, convert custom API object to LiveTime object and add to list.
                    services.append(LiveTime(service))

            services.sort(key=lambda x: x.TimeInMin())
            return services
        except Exception as e:
            print("GetData() ERROR")
            print(str(e))
            return []


def main():
    # Display dimensions
    WIDTH = 256
    HEIGHT = 64
    HEADER_HEIGHT = 16  # For Line1
    SCROLLING_HEIGHT = 48  # For Lines 2-10 (3 lines visible at a time)


    try:
        # Initialize the emulator device
        GPIO.setwarnings(False)
        serial = spi(port=0)

        device = ssd1322(serial, mode="1", rotate=0)

        # Create scrolling text lines (Lines 2-10 only)
        scrolling_lines = ScrollingTextLines(width=WIDTH, height=144)  # 9 lines * 16px

        # Fetch live data from API
        real_time_data = []
        services = obtain_realtime_data()
        scrolling_lines.set_line_data(services)
        print("\nControls:")
        print("- Press ESC to exit")
        print("- Close window to exit")
        print("\nStarting display...")
        frame_count = 0
        update_interval = 0.033  # Update every 33ms (~30 FPS)
        last_status_time = time.time()
        last_request_time = time.time()
        while True:
            start_time = time.time()
            scrolling_lines.update()
            header_img, scroll_img = scrolling_lines.get_visible_portion()
            with canvas(device) as draw:
                if header_img is not None:
                    draw.bitmap((0, 0), header_img, fill="white")
                if scroll_img is not None:
                    draw.bitmap((0, HEADER_HEIGHT), scroll_img, fill="white")
            elapsed = time.time() - start_time
            if elapsed < update_interval:
                time.sleep(update_interval - elapsed)
            frame_count += 1

            # Show status every 2 seconds
            current_time = time.time()
            if current_time - last_status_time >= 2.0:
                lines = scrolling_lines.get_current_lines()
                state_desc = scrolling_lines.get_state_description()
                scrolling = "SCROLLING" if scrolling_lines.is_scrolling else "PAUSED"
                current_data = scrolling_lines.get_current_data()

                if header_img is not None and len(scrolling_lines.line_data) > 0:
                    line1_data = scrolling_lines.line_data[0]
                    # Accept ArrivalData or tuple
                    print(f"\nFrame: {frame_count}")
                    print(f"Fixed Header (Line1): Line1 | {line1_data.LineName} | {line1_data.Destination} | {line1_data.DisplayTime}")
                else:
                    print(f"\nFrame: {frame_count}")
                    print("No Departures Planned")
                print(f"Displaying in scroll area: {lines} ({state_desc})")
                print(f"Status: {scrolling}")
                print("Current scroll area data:")
                for line_num, line_text, destination, time_str in current_data:
                    print(f"  Line{line_num} | {line_text:<15} | {destination:<10} | {time_str:>8}")

                last_status_time = current_time

            if current_time - last_request_time >= 60.0:
                # Fetch new data from API every 60 seconds
                services = obtain_realtime_data()
                scrolling_lines.set_line_data(services)
                last_request_time = current_time

    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def obtain_realtime_data() -> list[Any]:
    services = LiveTime.GetData(STATION_ID)
    services2 = LiveTime.GetData(STATION_ID2)
    services.extend(services2)
    services.sort(key=lambda x: x.TimeInMin())
    for svc in services:
        print(f"Service {svc.LineName} to {svc.Destination} arriving at {svc.ExptArrival} (in {svc.DisplayTime})")
    return services


if __name__ == "__main__":
    main()
