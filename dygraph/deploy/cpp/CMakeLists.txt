cmake_minimum_required(VERSION 3.0)
project(PaddleDeploy CXX C)

option(WITH_MKL        "Compile demo with MKL/OpenBlas support,defaultuseMKL."          ON)
option(WITH_GPU        "Compile demo with GPU/CPU, default use CPU."                    OFF)
if (NOT WIN32)
    option(WITH_STATIC_LIB "Compile demo with static/shared library, default use static."   OFF)
else()
    option(WITH_STATIC_LIB "Compile demo with static/shared library, default use static."   ON)
endif()
option(WITH_TENSORRT "Compile demo with TensorRT."   OFF)
option(WITH_ENCRYPTION "Compile demo with ENCRYPTION."   OFF)

SET(TENSORRT_DIR "" CACHE PATH "Location of libraries")
SET(PADDLE_DIR "" CACHE PATH "Location of libraries")
SET(OPENCV_DIR "" CACHE PATH "Location of libraries")
SET(CUDA_LIB "" CACHE PATH "Location of libraries")
SET(OPENSSL_DIR "" CACHE PATH "Location of libraries")

SET(PROJECT_ROOT_DIR  "." CACHE PATH  "root directory of project.")

if (NOT WIN32)
    set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/demo)
else()
    set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/paddle_deploy)
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/paddle_deploy)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/paddle_deploy)
    add_definitions(-DPADDLEX_DEPLOY)
endif()

#source
include_directories("${CMAKE_SOURCE_DIR}/")
link_directories("${CMAKE_CURRENT_BINARY_DIR}")

#yaml-cpp
if(WIN32)
  SET(YAML_BUILD_SHARED_LIBS OFF CACHE BOOL "yaml build shared library.")
else()
  SET(YAML_BUILD_SHARED_LIBS ON CACHE BOOL "yaml build shared library.")
endif(WIN32)
include(${PROJECT_ROOT_DIR}/cmake/yaml-cpp.cmake)
include_directories("${CMAKE_CURRENT_BINARY_DIR}/ext/yaml-cpp/src/ext-yaml-cpp/include")
link_directories("${CMAKE_CURRENT_BINARY_DIR}/ext/yaml-cpp/lib")

#paddle inference
if (NOT DEFINED PADDLE_DIR OR ${PADDLE_DIR} STREQUAL "")
    message(FATAL_ERROR "please set PADDLE_DIR with -DPADDLE_DIR=/path/paddle_influence_dir")
endif()

#paddle inference third party
include_directories("${PADDLE_DIR}")
include_directories("${PADDLE_DIR}/third_party/install/protobuf/include")
include_directories("${PADDLE_DIR}/third_party/install/glog/include")
include_directories("${PADDLE_DIR}/third_party/install/gflags/include")
include_directories("${PADDLE_DIR}/third_party/install/xxhash/include")
include_directories("${PADDLE_DIR}/third_party/install/cryptopp/include")

link_directories("${PADDLE_DIR}/paddle/lib/")
link_directories("${PADDLE_DIR}/third_party/install/protobuf/lib")
link_directories("${PADDLE_DIR}/third_party/install/glog/lib")
link_directories("${PADDLE_DIR}/third_party/install/gflags/lib")
link_directories("${PADDLE_DIR}/third_party/install/xxhash/lib")
link_directories("${PADDLE_DIR}/third_party/install/cryptopp/lib")

if (WIN32)
  set(DEPS ${DEPS} ${PADDLE_DIR}/paddle/lib/paddle_inference.lib)
  set(DEPS ${DEPS} glog gflags_static libprotobuf xxhash cryptopp-static libyaml-cppmt shlwapi)
else()
  if (WITH_STATIC_LIB)
    set(DEPS ${PADDLE_DIR}/paddle/lib/libpaddle_inference${CMAKE_STATIC_LIBRARY_SUFFIX})
  else()
    set(DEPS ${PADDLE_DIR}/paddle/lib/libpaddle_inference${CMAKE_SHARED_LIBRARY_SUFFIX})
  endif()
  set(DEPS ${DEPS} glog gflags protobuf xxhash cryptopp yaml-cpp)
endif(WIN32)

#MKL
if(WITH_MKL)
  ADD_DEFINITIONS(-DUSE_MKL)
  set(MKLML_PATH "${PADDLE_DIR}/third_party/install/mklml")
  include_directories("${MKLML_PATH}/include")
  if (WIN32)
    set(MATH_LIB ${MKLML_PATH}/lib/mklml.lib ${MKLML_PATH}/lib/libiomp5md.lib)
  else ()
    set(MATH_LIB ${MKLML_PATH}/lib/libmklml_intel${CMAKE_SHARED_LIBRARY_SUFFIX} ${MKLML_PATH}/lib/libiomp5${CMAKE_SHARED_LIBRARY_SUFFIX})
    execute_process(COMMAND cp -r ${MKLML_PATH}/lib/libmklml_intel${CMAKE_SHARED_LIBRARY_SUFFIX} /usr/lib)
  endif ()
  set(MKLDNN_PATH "${PADDLE_DIR}/third_party/install/mkldnn")
  if(EXISTS ${MKLDNN_PATH})
    include_directories("${MKLDNN_PATH}/include")
    if (WIN32)
      set(MKLDNN_LIB ${MKLDNN_PATH}/lib/mkldnn.lib)
    else ()
      set(MKLDNN_LIB ${MKLDNN_PATH}/lib/libmkldnn.so.0)
    endif ()
  endif()
else()
  set(MATH_LIB ${PADDLE_DIR}/third_party/install/openblas/lib/libopenblas${CMAKE_STATIC_LIBRARY_SUFFIX})
endif()

set(DEPS ${DEPS} ${MATH_LIB} ${MKLDNN_LIB})

#OPENCV
if (NOT (${CMAKE_SYSTEM_PROCESSOR} STREQUAL "aarch64"))
  if (NOT DEFINED OPENCV_DIR OR ${OPENCV_DIR} STREQUAL "")
    message(FATAL_ERROR "please set OPENCV_DIR with -DOPENCV_DIR=/path/opencv")
  endif()
endif()

if (WIN32)
  find_package(OpenCV REQUIRED PATHS ${OPENCV_DIR}/build/ NO_DEFAULT_PATH)
  unset(OpenCV_DIR CACHE)
else ()
  if (${CMAKE_SYSTEM_PROCESSOR} STREQUAL "aarch64") # x86_64 aarch64
    set(OpenCV_INCLUDE_DIRS "/usr/include/opencv4")
    file(GLOB OpenCV_LIBS /usr/lib/aarch64-linux-gnu/libopencv_*${CMAKE_SHARED_LIBRARY_SUFFIX})
    message("OpenCV libs: ${OpenCV_LIBS}")
  else()
    find_package(OpenCV REQUIRED PATHS ${OPENCV_DIR}/share/OpenCV NO_DEFAULT_PATH)
  endif()
endif ()

set(DEPS ${DEPS} ${OpenCV_LIBS})
include_directories(${OpenCV_INCLUDE_DIRS})


if (WITH_TENSORRT AND WITH_GPU)
  include_directories("${TENSORRT_DIR}/include")
  link_directories("${TENSORRT_DIR}/lib")

  file(READ ${TENSORRT_DIR}/include/NvInfer.h TENSORRT_VERSION_FILE_CONTENTS)
  string(REGEX MATCH "define NV_TENSORRT_MAJOR +([0-9]+)" TENSORRT_MAJOR_VERSION
    "${TENSORRT_VERSION_FILE_CONTENTS}")
  if("${TENSORRT_MAJOR_VERSION}" STREQUAL "")
    file(READ ${TENSORRT_DIR}/include/NvInferVersion.h TENSORRT_VERSION_FILE_CONTENTS)
    string(REGEX MATCH "define NV_TENSORRT_MAJOR +([0-9]+)" TENSORRT_MAJOR_VERSION
      "${TENSORRT_VERSION_FILE_CONTENTS}")
  endif()
  if("${TENSORRT_MAJOR_VERSION}" STREQUAL "")
    message(SEND_ERROR "Failed to detect TensorRT version.")
  endif()
  string(REGEX REPLACE "define NV_TENSORRT_MAJOR +([0-9]+)" "\\1"
    TENSORRT_MAJOR_VERSION "${TENSORRT_MAJOR_VERSION}")
  message(STATUS "Current TensorRT header is ${TENSORRT_INCLUDE_DIR}/NvInfer.h. "
    "Current TensorRT version is v${TENSORRT_MAJOR_VERSION}. ")
endif()

#set GPU
if(WITH_GPU)
  if (NOT DEFINED CUDA_LIB OR ${CUDA_LIB} STREQUAL "")
    message(FATAL_ERROR "please set CUDA_LIB with -DCUDA_LIB=/path/cuda/lib64")
  endif()

  if(NOT WIN32)
    if (NOT DEFINED CUDNN_LIB)
      message(FATAL_ERROR "please set CUDNN_LIB with -DCUDNN_LIB=/path/cudnn/")
    endif()

    set(DEPS ${DEPS} ${CUDA_LIB}/libcudart${CMAKE_SHARED_LIBRARY_SUFFIX})
    set(DEPS ${DEPS} ${CUDNN_LIB}/libcudnn${CMAKE_SHARED_LIBRARY_SUFFIX})

    if (WITH_TENSORRT)
      set(DEPS ${DEPS} ${TENSORRT_DIR}/lib/libnvinfer${CMAKE_SHARED_LIBRARY_SUFFIX})
      set(DEPS ${DEPS} ${TENSORRT_DIR}/lib/libnvinfer_plugin${CMAKE_SHARED_LIBRARY_SUFFIX})
    endif()

  else()
    set(DEPS ${DEPS} ${CUDA_LIB}/cudart${CMAKE_STATIC_LIBRARY_SUFFIX} )
    set(DEPS ${DEPS} ${CUDA_LIB}/cublas${CMAKE_STATIC_LIBRARY_SUFFIX} )
    set(DEPS ${DEPS} ${CUDA_LIB}/cudnn${CMAKE_STATIC_LIBRARY_SUFFIX})

    if (WITH_TENSORRT)
      set(DEPS ${DEPS} ${TENSORRT_DIR}/lib/nvinfer${CMAKE_STATIC_LIBRARY_SUFFIX})
      set(DEPS ${DEPS} ${TENSORRT_DIR}/lib/nvinfer_plugin${CMAKE_STATIC_LIBRARY_SUFFIX})
      if(${TENSORRT_MAJOR_VERSION} GREATER_EQUAL 7)
        set(DEPS ${DEPS} ${TENSORRT_DIR}/lib/myelin64_1${CMAKE_STATIC_LIBRARY_SUFFIX})
      endif()
    endif()
  endif()
endif()

macro(safe_set_static_flag)
    foreach(flag_var
        CMAKE_CXX_FLAGS CMAKE_CXX_FLAGS_DEBUG CMAKE_CXX_FLAGS_RELEASE
        CMAKE_CXX_FLAGS_MINSIZEREL CMAKE_CXX_FLAGS_RELWITHDEBINFO)
      if(${flag_var} MATCHES "/MD")
        string(REGEX REPLACE "/MD" "/MT" ${flag_var} "${${flag_var}}")
      endif(${flag_var} MATCHES "/MD")
    endforeach(flag_var)
endmacro()

if (WIN32)
    add_definitions("/DGOOGLE_GLOG_DLL_DECL=")
    find_package(OpenMP REQUIRED)
    if (OPENMP_FOUND)
        message("OPENMP FOUND")
        set(CMAKE_C_FLAGS_DEBUG   "${CMAKE_C_FLAGS_DEBUG} ${OpenMP_C_FLAGS}")
        set(CMAKE_C_FLAGS_RELEASE  "${CMAKE_C_FLAGS_RELEASE} ${OpenMP_C_FLAGS}")
        set(CMAKE_CXX_FLAGS_DEBUG  "${CMAKE_CXX_FLAGS_DEBUG} ${OpenMP_CXX_FLAGS}")
        set(CMAKE_CXX_FLAGS_RELEASE   "${CMAKE_CXX_FLAGS_RELEASE} ${OpenMP_CXX_FLAGS}")
    endif()
    set(CMAKE_C_FLAGS_DEBUG   "${CMAKE_C_FLAGS_DEBUG} /bigobj /MTd")
    set(CMAKE_C_FLAGS_RELEASE  "${CMAKE_C_FLAGS_RELEASE} /bigobj /MT")
    set(CMAKE_CXX_FLAGS_DEBUG  "${CMAKE_CXX_FLAGS_DEBUG} /bigobj /MTd")
    set(CMAKE_CXX_FLAGS_RELEASE   "${CMAKE_CXX_FLAGS_RELEASE} /bigobj /MT")
    if (WITH_STATIC_LIB)
        safe_set_static_flag()
        add_definitions(-DSTATIC_LIB)
    endif()
else()
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -g -o3 -fopenmp -std=c++11")
    set(CMAKE_STATIC_LIBRARY_PREFIX "")
    set(EXTERNAL_LIB "-ldl -lrt -lgomp -lz -lm -lpthread")
    set(DEPS ${DEPS} ${EXTERNAL_LIB})
endif()

message("-----DEPS = ${DEPS}")

#project
include_directories("${PROJECT_ROOT_DIR}")

aux_source_directory(${PROJECT_ROOT_DIR}/model_deploy/common/src SRC)
set(ENGINE_SRC ${PROJECT_ROOT_DIR}/model_deploy/engine/src/ppinference_engine.cpp)

#detector seg
aux_source_directory(${PROJECT_ROOT_DIR}/model_deploy/ppdet/src DETECTOR_SRC)
aux_source_directory(${PROJECT_ROOT_DIR}/model_deploy/ppseg/src DETECTOR_SRC)
aux_source_directory(${PROJECT_ROOT_DIR}/model_deploy/ppclas/src DETECTOR_SRC)
aux_source_directory(${PROJECT_ROOT_DIR}/model_deploy/paddlex/src DETECTOR_SRC)

set(ENCRYPTION_SRC "")
if (WITH_ENCRYPTION)
  add_definitions(-DPADDLEX_DEPLOY_ENCRYPTION)
  set(CMAKE_C_FLAGS "-fPIC ${CMAKE_C_FLAGS}")
  set(CMAKE_CXX_FLAGS "-fPIC ${CMAKE_CXX_FLAGS}")

  include_directories("${OPENSSL_DIR}/install-${CMAKE_SYSTEM_PROCESSOR}/include")
  link_directories("${OPENSSL_DIR}/install-${CMAKE_SYSTEM_PROCESSOR}/lib")
  if (WIN32)
    set(DEPS ${DEPS} libssl_static${CMAKE_STATIC_LIBRARY_SUFFIX} libcrypto_static${CMAKE_STATIC_LIBRARY_SUFFIX})
  else ()
    set(DEPS ${DEPS} libssl${CMAKE_STATIC_LIBRARY_SUFFIX} libcrypto${CMAKE_STATIC_LIBRARY_SUFFIX})
   endif()
  aux_source_directory(${PROJECT_ROOT_DIR}/encryption/src ENCRYPTION_SRC)
  aux_source_directory(${PROJECT_ROOT_DIR}/encryption/util/src ENCRYPTION_SRC)
  aux_source_directory(${PROJECT_ROOT_DIR}/encryption/util/src/crypto ENCRYPTION_SRC)
endif()

add_executable(model_infer ${PROJECT_ROOT_DIR}/demo/model_infer.cpp ${SRC} ${ENGINE_SRC} ${DETECTOR_SRC} ${ENCRYPTION_SRC})
ADD_DEPENDENCIES(model_infer ext-yaml-cpp)
target_link_libraries(model_infer ${DEPS})

add_executable(batch_infer ${PROJECT_ROOT_DIR}/demo/batch_infer.cpp ${SRC} ${ENGINE_SRC} ${DETECTOR_SRC} ${ENCRYPTION_SRC})
ADD_DEPENDENCIES(batch_infer ext-yaml-cpp)
target_link_libraries(batch_infer ${DEPS})

add_executable(multi_gpu_model_infer ${PROJECT_ROOT_DIR}/demo/multi_gpu_model_infer.cpp ${SRC} ${ENGINE_SRC} ${DETECTOR_SRC} ${ENCRYPTION_SRC})
ADD_DEPENDENCIES(multi_gpu_model_infer ext-yaml-cpp)
target_link_libraries(multi_gpu_model_infer ${DEPS})

if (WITH_TENSORRT)
  add_executable(tensorrt_infer ${PROJECT_ROOT_DIR}/demo/tensorrt_infer.cpp ${SRC} ${ENGINE_SRC} ${DETECTOR_SRC} ${ENCRYPTION_SRC})
  ADD_DEPENDENCIES(tensorrt_infer ext-yaml-cpp)
  target_link_libraries(tensorrt_infer ${DEPS})
endif()

if(WIN32)
  add_custom_command(TARGET model_infer POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy ${PADDLE_DIR}/third_party/install/mklml/lib/mklml.dll ${CMAKE_BINARY_DIR}/paddle_deploy
    COMMAND ${CMAKE_COMMAND} -E copy ${PADDLE_DIR}/third_party/install/mklml/lib/libiomp5md.dll ${CMAKE_BINARY_DIR}/paddle_deploy
    COMMAND ${CMAKE_COMMAND} -E copy ${PADDLE_DIR}/third_party/install/mkldnn/lib/mkldnn.dll  ${CMAKE_BINARY_DIR}/paddle_deploy
    COMMAND ${CMAKE_COMMAND} -E copy ${PADDLE_DIR}/paddle/lib/paddle_inference.dll ${CMAKE_BINARY_DIR}/paddle_deploy
  )
  if (WITH_TENSORRT)
    add_custom_command(TARGET model_infer POST_BUILD
      COMMAND ${CMAKE_COMMAND} -E copy ${TENSORRT_DIR}/lib/nvinfer.dll ${CMAKE_BINARY_DIR}/paddle_deploy
      COMMAND ${CMAKE_COMMAND} -E copy ${TENSORRT_DIR}/lib/nvinfer_plugin.dll ${CMAKE_BINARY_DIR}/paddle_deploy
    )
    if(${TENSORRT_MAJOR_VERSION} GREATER_EQUAL 7)
      add_custom_command(TARGET model_infer POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy ${TENSORRT_DIR}/lib/myelin64_1.dll ${CMAKE_BINARY_DIR}/paddle_deploy
      )
    endif()
  endif()
endif()
