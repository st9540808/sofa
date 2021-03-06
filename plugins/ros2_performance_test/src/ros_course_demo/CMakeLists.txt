cmake_minimum_required(VERSION 3.5)
project(ros_course_demo)

set(CMAKE_VERBOSE_MAKEFILE on)

# Default to C99
if(NOT CMAKE_C_STANDARD)
  set(CMAKE_C_STANDARD 99)
endif()

# Default to C++14
if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-g -Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
# uncomment the following section in order to fill in
# further dependencies manually.
# find_package(<dependency> REQUIRED)

find_package(rclcpp REQUIRED)
find_package(rclcpp_components REQUIRED)
find_package(rcutils)
find_package(rmw REQUIRED)
find_package(std_msgs REQUIRED)
# find_package(rosidl_default_generators REQUIRED)

function(custom_executable target)
  add_executable(${target} src/${target}.cpp)
  ament_target_dependencies(${target}
    "rclcpp"
    "rcutils"
    "std_msgs")
  install(TARGETS ${target}
  DESTINATION lib/${PROJECT_NAME})
endfunction()

custom_executable(listener)
custom_executable(talker)
custom_executable(rtt_sender)
custom_executable(rtt_receiver)

# include_directories(./../../install/ros_course_demo/include)
# set(msg_files "msg/Custom.msg")
# rosidl_generate_interfaces(${PROJECT_NAME} ${msg_files})
# ament_export_dependencies(rosidl_default_runtime)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # the following line skips the linter which checks for copyrights
  # uncomment the line when a copyright and license is not present in all source files
  #set(ament_cmake_copyright_FOUND TRUE)
  # the following line skips cpplint (only works in a git repo)
  # uncomment the line when this package is not in a git repo
  #set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
