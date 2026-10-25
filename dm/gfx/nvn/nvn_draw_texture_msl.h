// A fixed-function rectangle uses window/texel coordinates and no game shaders.
#pragma once
static const char *k_nvn_draw_texture_msl =
    "#include <metal_stdlib>\nusing namespace metal;\n"
    "struct DrawTextureVertex { float4 position [[position]]; };\n"
    "vertex DrawTextureVertex draw_texture_vs(uint id [[vertex_id]], constant float4 *p [[buffer(0)]]) {\n"
    "  float2 corner = float2(id & 1u, id >> 1u);\n"
    "  float2 xy = p[0].xy + corner * p[0].zw;\n"
    "  DrawTextureVertex o; o.position = float4(xy * p[2].xy * float2(2, -2) + float2(-1, 1), 0, 1); return o;\n"
    "}\n"
    "fragment float4 draw_texture_fs(DrawTextureVertex in [[stage_in]], texture2d<float> t [[texture(0)]], sampler s [[sampler(0)]], constant float4 *p [[buffer(0)]]) {\n"
    "  float2 texel = p[1].xy + (in.position.xy - p[0].xy) * p[1].zw;\n"
    "  return t.sample(s, texel * p[2].zw, bias(p[3].x));\n"
    "}\n";
