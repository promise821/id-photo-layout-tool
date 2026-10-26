# -*- coding: utf-8 -*-
import config as c

print("规格:")
for k in c.MIX_KEYS:
    s = c.SPECS[k]
    print("  %s %dx%d  (%s)" % (c.MIX_LABELS[k], s.width_px, s.height_px, k))
print("couple", c.SPECS["couple"].width_px, c.SPECS["couple"].height_px)
print("single", c.SPECS["single"].width_px, c.SPECS["single"].height_px,
      "rotate", c.SPECS["single"].rotate_cw)
print("模板:", list(c.TEMPLATES))
print("normalize_mix(旧3值) =", c.normalize_mix("3,2,2"))
print("normalize_mix(带括号) =", c.normalize_mix("(0, 3, 0, 2, 2, 0)"))
print("normalize_mix(新6值) =", c.normalize_mix("0,3,0,2,2,0"))
print("normalize_mix([0,3,0,2,2,0]) =", c.normalize_mix([0, 3, 0, 2, 2, 0]))
print("mix_people(旧3值) =", c.normalize_mix_people("p1,p2,p3"))
s = c.load_settings()
print("默认设置 mix =", s["mix"], s["mix_people"])
for t in c.TEMPLATES.values():
    print("  [%s] %s  builder=%s spec=%s count=%s max=%s" %
          (t.key, t.title, t.builder, t.spec_key, t.default_count, t.max_count))
