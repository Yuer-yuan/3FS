#pragma once

// Keep production headers independent of GoogleTest's include path while
// preserving the exact FRIEND_TEST expansion used by gtest/gtest_prod.h.
#ifndef FRIEND_TEST
#define FRIEND_TEST(test_case_name, test_name) friend class test_case_name##_##test_name##_Test
#endif
