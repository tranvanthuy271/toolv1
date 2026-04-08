"""
arr_data_editor.py – Binary parser / writer for arr_data_game

Reads the ZLIB-compressed sequential binary, parses characterData,
characterDataProcessors, and H[] (SkillLevelCalculator), and stores raw
bytes before/after these sections so the file can be saved after deep edits.
"""

import io
import os
import shutil
import struct
import zlib
from dataclasses import dataclass, field
from typing import List


# ---------------------------------------------------------------------------
# Java-compatible binary reader / writer
# Uses the game's custom readUTF/writeUTF that encodes short strings via a
# charset lookup table and falls back to standard Java modified UTF-8 only
# for empty strings or strings longer than 255 characters.
# ---------------------------------------------------------------------------

CHARSET = (' 0123456789+-*=\'"\\/_?.,\u02cb\u02ca~\u02c0:;|<>[]{}!@#$%^&*()'
           'a\u00e1\u00e0\u1ea3\u00e3\u1ea1\u00e2\u1ea5\u1ea7\u1ea9\u1eab\u1ead'
           '\u0103\u1eaf\u1eb1\u1eb3\u1eb5\u1eb7bcd\u0111'
           'e\u00e9\u00e8\u1ebb\u1ebd\u1eb9\u00ea\u1ebf\u1ec1\u1ec3\u1ec5\u1ec7'
           'fghi\u00ed\u00ec\u1ec9\u0129\u1ecbjklmn'
           'o\u00f3\u00f2\u1ecf\u00f5\u1ecd\u00f4\u1ed1\u1ed3\u1ed5\u1ed7\u1ed9'
           '\u01a1\u1edb\u1edd\u1edf\u1ee1\u1ee3pqrst'
           'u\u00fa\u00f9\u1ee7\u0169\u1ee5\u01b0\u1ee9\u1eeb\u1eed\u1eef\u1ef1'
           'vxy\u00fd\u1ef3\u1ef7\u1ef9\u1ef5zw'
           'A\u00c1\u00c0\u1ea2\u00c3\u1ea0\u00c2\u1ea4\u1ea6\u1ea8\u1eaa\u1eac'
           '\u0102\u1eae\u1eb0\u1eb2\u1eb4\u1eb6BCD\u0110'
           'E\u00c9\u00c8\u1eba\u1ebc\u1eb8\u00ca\u1ebe\u1ec0\u1ec2\u1ec4\u1ec6'
           'FGHI\u00cd\u00cc\u1ec8\u0128\u1ecaJKLMN'
           'O\u00d3\u00d2\u1ece\u00d5\u1ecc\u00d4\u1ed0\u1ed2\u1ed4\u1ed6\u1ed8'
           '\u01a0\u1eda\u1edc\u1ede\u1ee0\u1ee2PQRST'
           'U\u00da\u00d9\u1ee6\u0168\u1ee4\u01af\u1ee8\u1eea\u1eec\u1eee\u1ef0'
           'VXY\u00dd\u1ef2\u1ef6\u1ef8\u1ef4ZW')


class JavaBinaryReader:
    """Mirrors Java DataInputStream – big-endian, with custom readUTF."""

    def __init__(self, data: bytes):
        self._stream = io.BytesIO(data)

    @property
    def position(self) -> int:
        return self._stream.tell()

    @position.setter
    def position(self, pos: int):
        self._stream.seek(pos)

    def read_byte(self) -> int:
        return struct.unpack('>b', self._stream.read(1))[0]

    def read_unsigned_byte(self) -> int:
        return struct.unpack('>B', self._stream.read(1))[0]

    def read_short(self) -> int:
        return struct.unpack('>h', self._stream.read(2))[0]

    def read_unsigned_short(self) -> int:
        return struct.unpack('>H', self._stream.read(2))[0]

    def read_int(self) -> int:
        return struct.unpack('>i', self._stream.read(4))[0]

    def read_boolean(self) -> bool:
        return self._stream.read(1)[0] != 0

    def read_utf(self) -> str:
        """Custom readUTF matching the game's Reader.readUTF().

        Format: 1 byte prefix.
          - If 0  -> standard Java modified UTF-8 (2-byte length + data)
          - If >0 -> that many charset-indexed bytes follow
        """
        first = self.read_unsigned_byte()
        if first == 0:
            length = self.read_unsigned_short()
            data = self._stream.read(length)
            return _decode_java_utf8(data)
        else:
            chars = []
            for _ in range(first):
                idx = self.read_unsigned_byte()
                if 0 <= idx < len(CHARSET):
                    chars.append(CHARSET[idx])
                else:
                    chars.append(' ')
            return ''.join(chars)


class JavaBinaryWriter:
    """Mirrors Java DataOutputStream – big-endian, with custom writeUTF."""

    def __init__(self):
        self._stream = io.BytesIO()

    def get_bytes(self) -> bytes:
        return self._stream.getvalue()

    def write_byte(self, value: int):
        self._stream.write(struct.pack('>b', value))

    def write_unsigned_byte(self, value: int):
        self._stream.write(struct.pack('>B', value))

    def write_short(self, value: int):
        self._stream.write(struct.pack('>h', value))

    def write_unsigned_short(self, value: int):
        self._stream.write(struct.pack('>H', value))

    def write_utf(self, text: str):
        """Custom writeUTF matching the game's Writer.writeUTF().

        If 1 <= len(text) <= 255 -> custom charset encoding.
        Otherwise (empty or >255) -> 0x00 + standard Java writeUTF.
        """
        if 0 < len(text) <= 255:
            self._stream.write(struct.pack('>B', len(text)))
            for ch in text:
                idx = CHARSET.find(ch)
                if idx < 0:
                    idx = 0
                self._stream.write(struct.pack('>B', idx))
        else:
            self._stream.write(b'\x00')
            data = _encode_java_utf8(text)
            self.write_unsigned_short(len(data))
            self._stream.write(data)


# ---------------------------------------------------------------------------
# Java modified UTF-8 codec
# ---------------------------------------------------------------------------

def _decode_java_utf8(data: bytes) -> str:
    result = []
    i = 0
    while i < len(data):
        b = data[i]
        if b & 0x80 == 0:
            result.append(chr(b))
            i += 1
        elif b & 0xE0 == 0xC0:
            c = ((b & 0x1F) << 6) | (data[i + 1] & 0x3F)
            result.append(chr(c))
            i += 2
        elif b & 0xF0 == 0xE0:
            c = ((b & 0x0F) << 12) | ((data[i + 1] & 0x3F) << 6) | (data[i + 2] & 0x3F)
            result.append(chr(c))
            i += 3
        else:
            result.append(chr(b))
            i += 1
    return ''.join(result)


def _encode_java_utf8(text: str) -> bytes:
    result = bytearray()
    for ch in text:
        code = ord(ch)
        if code == 0:
            result.extend(b'\xC0\x80')
        elif 1 <= code <= 0x7F:
            result.append(code)
        elif code <= 0x7FF:
            result.append(0xC0 | (code >> 6))
            result.append(0x80 | (code & 0x3F))
        else:
            result.append(0xE0 | (code >> 12))
            result.append(0x80 | ((code >> 6) & 0x3F))
            result.append(0x80 | (code & 0x3F))
    return bytes(result)


# ---------------------------------------------------------------------------
# Section-skip helpers  (advance past each section before H[])
# Order must match readArrDataGame() in DataCenter.java exactly.
# ---------------------------------------------------------------------------

def _skip_data_icon_char(r: JavaBinaryReader):
    for _ in range(r.read_byte()):
        r.read_short()


def _skip_data_name_class(r: JavaBinaryReader):
    for _ in range(r.read_byte()):
        r.read_utf()


def _skip_data_name_char(r: JavaBinaryReader):
    for _ in range(r.read_byte()):
        r.read_utf(); r.read_byte(); r.read_short()


def _skip_data_template_achievement(r: JavaBinaryReader):
    for _ in range(r.read_unsigned_byte()):
        r.read_byte(); r.read_utf()
        r.read_int(); r.read_int(); r.read_int(); r.read_int(); r.read_int()
        r.read_utf()


def _skip_task(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        r.read_utf()                                   # name
        r.read_short(); r.read_short(); r.read_short() # levelNeed, idNpc, idMap
        r.read_short(); r.read_short()                 # x, y
        r.read_utf(); r.read_utf(); r.read_utf()       # STR1-3
        r.read_int(); r.read_int(); r.read_int(); r.read_int()  # rewards
        r.read_utf()                                   # strItem
        step_count = r.read_byte()
        for _ in range(step_count):
            r.read_byte(); r.read_utf()
            r.read_short(); r.read_short(); r.read_short()  # idItem, idNpc, idMob
            r.read_short(); r.read_short(); r.read_short()  # idMap, x, y
            r.read_short()                                   # require
            r.read_utf(); r.read_utf()                       # STR, STR_ITEM


def _skip_data_task_day(r: JavaBinaryReader):
    for _ in range(r.read_unsigned_byte()):
        r.read_byte(); r.read_utf(); r.read_short()


def _skip_map_template(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        r.read_utf(); r.read_unsigned_byte(); r.read_byte()


def _skip_item_option_template(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        r.read_utf(); r.read_byte(); r.read_byte(); r.read_utf()


def _skip_effect_template(r: JavaBinaryReader):
    for _ in range(r.read_byte()):
        r.read_utf(); r.read_utf(); r.read_unsigned_byte()
        r.read_short(); r.read_short()


def _skip_item_template(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        r.read_utf(); r.read_utf(); r.read_boolean()
        r.read_byte(); r.read_byte(); r.read_byte()
        r.read_short(); r.read_unsigned_byte(); r.read_unsigned_short()
        r.read_short(); r.read_short()


def _skip_mob_template(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        r.read_short(); r.read_utf(); r.read_utf()
        r.read_unsigned_byte(); r.read_byte(); r.read_byte(); r.read_byte()
        r.read_short(); r.read_short()
        r.read_utf(); r.read_utf()


def _skip_npc_template(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        r.read_utf(); r.read_utf(); r.read_short()
        r.read_int(); r.read_int(); r.read_short()


def _skip_skill_template(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        r.read_utf(); r.read_utf(); r.read_short()
        r.read_byte(); r.read_byte(); r.read_byte(); r.read_short()


def _skip_skill(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        r.read_short(); r.read_short(); r.read_byte(); r.read_unsigned_byte()
        r.read_short(); r.read_int()
        r.read_short(); r.read_short(); r.read_byte()
        r.read_utf()


def _skip_skill_clan(r: JavaBinaryReader):
    for _ in range(r.read_unsigned_byte()):
        r.read_utf(); r.read_utf(); r.read_unsigned_byte()
        r.read_utf(); r.read_short(); r.read_int()


def _skip_data_type_item_body(r: JavaBinaryReader):
    for _ in range(r.read_byte()):
        r.read_byte()


def _skip_hashtable1(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        for _ in range(r.read_short()):
            r.read_short(); r.read_unsigned_byte(); r.read_unsigned_byte()
            r.read_short(); r.read_short()


def _skip_hashtable2(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        r.read_short(); r.read_short(); r.read_short()


def _skip_coordinate_data(r: JavaBinaryReader):
    for _ in range(r.read_unsigned_byte()):
        r.read_short(); r.read_short(); r.read_short(); r.read_byte()


def _skip_af_bytes(r: JavaBinaryReader):
    for _ in range(r.read_byte()):
        for _ in range(r.read_byte()):
            r.read_byte()


def _skip_character_data(r: JavaBinaryReader):
    frame_count = r.read_unsigned_byte()
    for _ in range(r.read_short()):
        r.read_byte()  # partType
        for _ in range(frame_count):
            icon_id = r.read_unsigned_short()
            if icon_id != 0:
                r.read_byte(); r.read_byte()  # rotationFrame, hueFlag
                r.read_byte(); r.read_byte()  # offsetX, offsetY


def _skip_character_data_processors(r: JavaBinaryReader):
    for _ in range(r.read_short()):
        length = r.read_byte()
        for _ in range(length):
            r.read_short()


@dataclass
class AnimationFrame:
    icon_id: int
    rotation_frame: int = 0
    hue_flag: int = 0
    offset_x: int = 0
    offset_y: int = 0


@dataclass
class CharacterDataEntry:
    part_type: int
    animation_frames: List[AnimationFrame] = field(default_factory=list)


@dataclass
class CharacterDataProcessorEntry:
    character_data_ids: List[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# H[] entry data model
# ---------------------------------------------------------------------------

@dataclass
class HEntry:
    """One entry in the H[] / SkillLevelCalculator array."""
    a0: List[int] = field(default_factory=list)   # processorLookupTable[0]
    a1: List[int] = field(default_factory=list)   # processorLookupTable[1]
    a2: List[int] = field(default_factory=list)   # processorLookupTable[2]


@dataclass
class ArrDataGame:
    """Parsed binary with raw bytes preserved around editable sections."""
    raw_before: bytes = b""
    character_frame_count: int = 0
    character_data: List[CharacterDataEntry] = field(default_factory=list)
    character_data_processors: List[CharacterDataProcessorEntry] = field(default_factory=list)
    h_entries: List[HEntry] = field(default_factory=list)
    raw_after: bytes = b""
    original_path: str = ""


# ---------------------------------------------------------------------------
# H[] read / write
# ---------------------------------------------------------------------------

def _parse_csv_shorts(s: str) -> List[int]:
    if not s or not s.strip():
        return []
    result = []
    for p in s.split(","):
        p = p.strip()
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    return result


def _read_character_data(r: JavaBinaryReader) -> tuple[int, List[CharacterDataEntry]]:
    frame_count = r.read_unsigned_byte()
    count = r.read_short()
    entries = []
    for _ in range(count):
        part_type = r.read_byte()
        frames = []
        for _ in range(frame_count):
            icon_id = r.read_unsigned_short()
            rotation_frame = 0
            hue_flag = 0
            offset_x = 0
            offset_y = 0
            if icon_id != 0:
                rotation_frame = r.read_byte()
                hue_flag = r.read_byte()
                offset_x = r.read_byte()
                offset_y = r.read_byte()
            frames.append(AnimationFrame(
                icon_id=icon_id,
                rotation_frame=rotation_frame,
                hue_flag=hue_flag,
                offset_x=offset_x,
                offset_y=offset_y,
            ))
        entries.append(CharacterDataEntry(
            part_type=part_type,
            animation_frames=frames,
        ))
    return frame_count, entries


def _read_character_data_processors(r: JavaBinaryReader) -> List[CharacterDataProcessorEntry]:
    count = r.read_short()
    entries = []
    for _ in range(count):
        length = r.read_byte()
        entries.append(CharacterDataProcessorEntry(
            character_data_ids=[r.read_short() for _ in range(length)],
        ))
    return entries


def _read_h_array(r: JavaBinaryReader) -> List[HEntry]:
    count = r.read_short()
    entries = []
    for _ in range(count):
        utf0 = r.read_utf()
        utf1 = r.read_utf()
        utf2 = r.read_utf()
        entries.append(HEntry(
            a0=_parse_csv_shorts(utf0),
            a1=_parse_csv_shorts(utf1),
            a2=_parse_csv_shorts(utf2),
        ))
    return entries


def _serialize_character_data(frame_count: int, entries: List[CharacterDataEntry]) -> bytes:
    w = JavaBinaryWriter()
    w.write_unsigned_byte(frame_count)
    w.write_short(len(entries))
    for entry in entries:
        if len(entry.animation_frames) != frame_count:
            raise ValueError(
                f"CharacterData partType={entry.part_type} has {len(entry.animation_frames)} "
                f"frames but expected {frame_count}."
            )
        w.write_byte(entry.part_type)
        for frame in entry.animation_frames:
            if not 0 <= frame.icon_id <= 65535:
                raise ValueError(
                    f"iconId {frame.icon_id} is outside the supported unsigned-short range 0..65535."
                )
            w.write_unsigned_short(frame.icon_id)
            if frame.icon_id != 0:
                w.write_byte(frame.rotation_frame)
                w.write_byte(frame.hue_flag)
                w.write_byte(frame.offset_x)
                w.write_byte(frame.offset_y)
    return w.get_bytes()


def _serialize_character_data_processors(entries: List[CharacterDataProcessorEntry]) -> bytes:
    w = JavaBinaryWriter()
    w.write_short(len(entries))
    for entry in entries:
        if len(entry.character_data_ids) > 127:
            raise ValueError(
                "CharacterDataProcessor cannot contain more than 127 ids in this format."
            )
        w.write_byte(len(entry.character_data_ids))
        for char_data_id in entry.character_data_ids:
            w.write_short(char_data_id)
    return w.get_bytes()


def _serialize_h_array(entries: List[HEntry]) -> bytes:
    w = JavaBinaryWriter()
    w.write_short(len(entries))
    for entry in entries:
        w.write_utf(",".join(str(v) for v in entry.a0))
        w.write_utf(",".join(str(v) for v in entry.a1))
        w.write_utf(",".join(str(v) for v in entry.a2))
    return w.get_bytes()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_arr_data_game(filepath: str) -> ArrDataGame:
    """Load and parse arr_data_game, extracting inspector data and H[] entries."""
    with open(filepath, 'rb') as f:
        compressed = f.read()

    decompressed = zlib.decompress(compressed)
    r = JavaBinaryReader(decompressed)

    # Skip sections 1-24 (everything before H[])
    _skip_data_icon_char(r)            # 1
    _skip_data_name_class(r)           # 2
    _skip_data_name_char(r)            # 3
    _skip_data_template_achievement(r) # 4
    _skip_task(r)                      # 5
    _skip_data_task_day(r)             # 6
    _skip_map_template(r)              # 7
    _skip_item_option_template(r)      # 8
    _skip_effect_template(r)           # 9
    _skip_item_template(r)             # 10
    _skip_mob_template(r)              # 11
    _skip_npc_template(r)              # 12
    _skip_skill_template(r)            # 13
    _skip_skill(r)                     # 14
    _skip_skill_clan(r)                # 15
    _skip_data_type_item_body(r)       # 16
    _skip_hashtable1(r)                # 17  (aj)
    _skip_hashtable2(r)                # 18  (al)
    _skip_hashtable1(r)                # 19  (ak)
    _skip_hashtable2(r)                # 20  (am)
    _skip_coordinate_data(r)           # 21
    _skip_af_bytes(r)                  # 22
    data_start = r.position
    character_frame_count, character_data = _read_character_data(r)              # 23
    character_data_processors = _read_character_data_processors(r) # 24

    h_start = r.position
    h_entries = _read_h_array(r)       # 25  H[] / SkillLevelCalculator
    h_end = r.position

    return ArrDataGame(
        raw_before=decompressed[:data_start],
        character_frame_count=character_frame_count,
        character_data=character_data,
        character_data_processors=character_data_processors,
        h_entries=h_entries,
        raw_after=decompressed[h_end:],
        original_path=filepath,
    )


def save_arr_data_game(arr_data: ArrDataGame, filepath: str,
                       backup: bool = True) -> int:
    """Save modified arr_data_game. Returns compressed size in bytes."""
    if backup and os.path.exists(filepath):
        shutil.copy2(filepath, filepath + '.bak')

    character_data_bytes = _serialize_character_data(
        arr_data.character_frame_count,
        arr_data.character_data,
    )
    processor_bytes = _serialize_character_data_processors(
        arr_data.character_data_processors,
    )
    h_bytes = _serialize_h_array(arr_data.h_entries)
    decompressed = (
        arr_data.raw_before
        + character_data_bytes
        + processor_bytes
        + h_bytes
        + arr_data.raw_after
    )
    compressed = zlib.compress(decompressed)

    with open(filepath, 'wb') as f:
        f.write(compressed)

    return len(compressed)
