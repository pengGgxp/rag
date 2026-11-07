"""Standalone renderer process: receives paths only, never application settings."""
import ctypes
import errno
import os
from pathlib import Path
import subprocess
import sys


def restrict(folder):
    if sys.platform != "linux":
        return
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (210 * 1024**2, 210 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    # Landlock keeps Office away from application files, .env and other jobs.
    libc = ctypes.CDLL(None, use_errno=True)
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled", ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed", ctypes.c_uint64), ("fd", ctypes.c_int)]
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 1:
        raise RuntimeError("filesystem sandbox unavailable")
    handled = (1 << 13)-1
    if abi >= 2: handled |= 1 << 13
    if abi >= 3: handled |= 1 << 14
    ruleset = Ruleset(handled)
    descriptor = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if descriptor < 0:
        raise RuntimeError("filesystem sandbox unavailable")
    try:
        for path, rights in [(folder, handled), ("/usr", 13), ("/lib", 13), ("/lib64", 13),
                ("/etc/fonts", 12), ("/etc/libreoffice", 12), ("/etc/ld.so.cache", 4),
                ("/etc/nsswitch.conf", 4), ("/etc/passwd", 4), ("/etc/group", 4), ("/var/cache/fontconfig", 12),
                ("/proc/cpuinfo", 4), ("/proc/meminfo", 4),
                ("/dev/null", 6), ("/dev/urandom", 4), ("/dev/random", 4)]:
            if not Path(path).exists(): continue
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(rights, fd)
                if libc.syscall(445, descriptor, 1, ctypes.byref(rule), 0) != 0:
                    raise RuntimeError("filesystem sandbox unavailable")
            finally:
                os.close(fd)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, descriptor, 0) != 0:
            raise RuntimeError("filesystem sandbox unavailable")
    finally:
        os.close(descriptor)
    # Inherited by LibreOffice and helpers; refuse socket networking in the kernel.
    seccomp = ctypes.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7fff0000)
    if not context:
        raise RuntimeError("sandbox unavailable")
    try:
        # Keep AF_UNIX available for local Office helper IPC, deny IPv4/IPv6 creation.
        class Arg(ctypes.Structure):
            _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint), ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]
        seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Arg)]
        for family in (2, 10):
            rule = Arg(0, 4, family, 0)
            if seccomp.seccomp_rule_add_array(context, 0x50000 | errno.EPERM,
                    seccomp.seccomp_syscall_resolve_name(b"socket"), 1, ctypes.byref(rule)) != 0:
                raise RuntimeError("sandbox unavailable")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("sandbox unavailable")
    finally:
        seccomp.seccomp_release(context)


def render(source, folder, kind, file_type):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000
    if file_type == "word":
        profile = folder / "profile"
        user = profile / "user"
        user.mkdir(parents=True)
        (user / "registrymodifications.xcu").write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item><item oor:path="/org.openoffice.Office.Common/Load"><prop oor:name="UpdateLinks" oor:op="fuse"><value>0</value></prop></item></oor:items>''')
        command = ["/usr/lib/libreoffice/program/soffice.bin", "-env:UserInstallation="+profile.as_uri(), "-env:OSL_SOCKET_PATH="+str(folder), "--headless", "--nologo",
            "--nodefault", "--norestore", "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(folder), str(source)]
        for attempt in range(2):
            result = subprocess.run(command, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # A fresh Office profile requests one bootstrap restart (normally handled by oosplash).
            if result.returncode != 81 or attempt:
                result.check_returncode()
                break
        pdf = source.with_suffix(".pdf")
        pdf.rename(folder / "document.pdf")
        source = folder / "document.pdf"
    if file_type in {"word", "pdf"}:
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(str(source)) as document:
            if len(document) == 0:
                raise ValueError("empty PDF")
            page = document[0]
            width, height = page.get_size()
            bitmap = page.render(scale=min(1.5, 800/max(width, height)))
            image = bitmap.to_pil().copy()
            bitmap.close(); page.close()
    else:
        with Image.open(source) as original:
            image = ImageOps.exif_transpose(original).convert("RGBA")
    image.thumbnail((640, 900), Image.Resampling.LANCZOS)
    image.save(folder / "thumbnail.webp", "WEBP", quality=82)
    image.close()


if __name__ == "__main__":
    restrict(Path(sys.argv[2]))
    render(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4])
