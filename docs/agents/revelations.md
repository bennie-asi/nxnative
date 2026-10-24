# Resident Evil Revelations: experimental bring-up

Title ID `0100643002136000`, update `v65536` (display version 1.0.1), SDK 1.6.0, MT Framework, NVN. This title has not been verified playable on macOS or iOS. Keep its provisional profile and all extracted files in the game's work directory until runtime behavior is measured.

The combined XCI contains both an Application CNMT and a Patch CNMT. Their Program NCAs share a header title ID, so selecting by header or file order can silently take the base. `nx_extract --inspect` catalogs the CNMT records; extraction pairs Program and Control by the selected record's content IDs. The base `main` build ID is `7b0e2611e8c9cae5a26e427362c72a8f`; update 1.0.1 is `99e56e5136a1316b0fe9e3fb6dd2dd2f`.

ARC version 9 has 64-byte paths and 84-byte entry records. The stored size, full raw size, separate flags and 32-bit offset are at entry offsets `0x44..0x53`. In particular, the shader member in `nativeNXx64/sa/NX_64/root.arc` expands to 651,519,592 bytes; ARC 7's 29-bit raw-size mask truncates it. Its SPK puts a `0x838`-byte control section before code, with metadata between records. Both ARC 7 and the newer code-before-control SPK layout remain supported.

The updated game's seven modules rewrite successfully. Two older compiler forms needed generic handling: `main+0x8bb5fc` bounds an offset-table index with `tbnz` and a low-bit `and`; `main+0x982008` schedules the table load 19 instructions before its branch. Inference checks register writes and table bounds rather than applying a title-specific address patch.

The extracted package contains 121,428 unique shaders (60,714 vertex, 60,714 pixel). All converted to SPIR-V with the checked-in translator, with zero failures. Metal compilation, native boot, rendered frames, memory use, input and audio still require measurement. A generated IPA is not evidence of playability; final iPhone validation must include the app's `Documents/log.txt`.

For a faster SDK initialization loop, **Probe native SDK startup** accepts an encrypted prepared-module bundle through the `CI_PROBE_JSON` secret (`url` and required `sha256`), encrypted with `IPA_PASSWORD`. The archive layout is `<title>_build/profile/<title>/` plus `<title>_build/patched_<version>/`. Supply the title ID and version as workflow inputs. Its encrypted diagnostic artifact records the startup log and summary. This probe can omit RomFS; asset failures after initialization are then expected, and cannot establish game or rendering compatibility. Full-file extraction and the normal IPA workflow remain required.
