// FoamMesh shim for Gmsh's GmshMessage.h: the lifted size-field code logs
// through Msg::Info / Warning / Error / Exit. Here they print to stdout /
// stderr with the same prefixes Gmsh's console uses, so the runtime's log
// parser sees one format for Gmsh and for foammesh-sizefield.
//
// GPL-3.0-or-later, (C) FoamMesh contributors. Not upstream code.

#ifndef FOAMMESH_GMSH_MESSAGE_SHIM_H
#define FOAMMESH_GMSH_MESSAGE_SHIM_H

#include <cstdarg>
#include <cstdio>
#include <cstdlib>

class Msg {
public:
  static void Info(const char *fmt, ...)
  {
    va_list args;
    va_start(args, fmt);
    std::fputs("Info    : ", stdout);
    std::vfprintf(stdout, fmt, args);
    std::fputc('\n', stdout);
    std::fflush(stdout);
    va_end(args);
  }
  static void Warning(const char *fmt, ...)
  {
    va_list args;
    va_start(args, fmt);
    std::fputs("Warning : ", stderr);
    std::vfprintf(stderr, fmt, args);
    std::fputc('\n', stderr);
    std::fflush(stderr);
    va_end(args);
  }
  static void Error(const char *fmt, ...)
  {
    va_list args;
    va_start(args, fmt);
    std::fputs("Error   : ", stderr);
    std::vfprintf(stderr, fmt, args);
    std::fputc('\n', stderr);
    std::fflush(stderr);
    va_end(args);
  }
  static void Exit(int code) { std::exit(code); }
};

#endif
