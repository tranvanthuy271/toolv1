import zlib
import struct
import io
import os
import sys
import shutil

sys.path.append(r'c:\Users\Thuy\Documents\LangLaServer\toolv1')
from arr_data_editor import JavaBinaryReader, JavaBinaryWriter

def write_int(self, value):
    self._stream.write(struct.pack('>i', value))
JavaBinaryWriter.write_int = write_int

path = r'c:\Users\Thuy\Documents\LangLaServer\LangLaServer\data\arr_data_game.bin'
with open(path, 'rb') as f:
    compressed = f.read()
decompressed = zlib.decompress(compressed)

r = JavaBinaryReader(decompressed)

from arr_data_editor import (
    _skip_data_icon_char, _skip_data_name_class, _skip_data_name_char, _skip_data_template_achievement,
    _skip_task, _skip_data_task_day, _skip_map_template, _skip_item_option_template, _skip_effect_template,
    _skip_item_template, _skip_mob_template, _skip_npc_template, _skip_skill_template
)

_skip_data_icon_char(r)
_skip_data_name_class(r)
_skip_data_name_char(r)
_skip_data_template_achievement(r)
_skip_task(r)
_skip_data_task_day(r)
_skip_map_template(r)
_skip_item_option_template(r)
_skip_effect_template(r)
_skip_item_template(r)
_skip_mob_template(r)
_skip_npc_template(r)
_skip_skill_template(r)

skills_start_pos = r.position

skills = []
count = r.read_short()
for _ in range(count):
    skill = {
        'id': r.read_short(),
        'idTemplate': r.read_short(),
        'level': r.read_byte(),
        'levelNeed': r.read_unsigned_byte(),
        'mpUse': r.read_short(),
        'coolDown': r.read_int(),
        'rangeNgang': r.read_short(),
        'rangeDoc': r.read_short(),
        'maxTarget': r.read_byte(),
        'strOptions': r.read_utf()
    }
    
    if skill['idTemplate'] == 4 and skill['level'] > 0:
        level = skill['level']
        atk = 1000 + (level - 1) * 200
        res = 120 + (level - 1) * 100
        evade = 55 + (level - 1) * 5
        
        skill['strOptions'] = f'78,{atk};181,{res};182,{evade}'
        print(f"Updated skill ID 4 level {level} -> {skill['strOptions']}")

    skills.append(skill)

skills_end_pos = r.position

w = JavaBinaryWriter()
w.write_short(len(skills))
for s in skills:
    w.write_short(s['id'])
    w.write_short(s['idTemplate'])
    w.write_byte(s['level'])
    w.write_unsigned_byte(s['levelNeed'])
    w.write_short(s['mpUse'])
    w.write_int(s['coolDown'])
    w.write_short(s['rangeNgang'])
    w.write_short(s['rangeDoc'])
    w.write_byte(s['maxTarget'])
    w.write_utf(s['strOptions'])

new_decompressed = decompressed[:skills_start_pos] + w.get_bytes() + decompressed[skills_end_pos:]
new_compressed = zlib.compress(new_decompressed)

shutil.copy2(path, path + '.bak_skill_patch')
with open(path, 'wb') as f:
    f.write(new_compressed)

print('Success: Patched arr_data_game.bin')
