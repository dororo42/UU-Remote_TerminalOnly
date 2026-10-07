set(CMAKE_C_COMPILE_OPTIONS_IPO "-flto=2;-fno-fat-lto-objects")
set(CMAKE_CXX_COMPILE_OPTIONS_IPO "-flto=2;-fno-fat-lto-objects")
# Directory compile options are not embedded in FreeRDP's CFLAGS build metadata.
foreach(kind file macro debug)
  add_compile_options("$<$<COMPILE_LANGUAGE:C,CXX>:-f${kind}-prefix-map=$ENV{UURB_PRODUCT_REPO}=/usr/src/uu-remote/product>"
                      "$<$<COMPILE_LANGUAGE:C,CXX>:-f${kind}-prefix-map=$ENV{UURB_SOURCE_BUILD_ROOT}=/usr/src/uu-remote/source>")
endforeach()
