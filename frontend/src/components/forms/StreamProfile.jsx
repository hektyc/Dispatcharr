// Modal.js
import React, { useEffect } from 'react';
import { useFormik } from 'formik';
import * as Yup from 'yup';
import API from '../../api';
import useUserAgentsStore from '../../store/userAgents';
import { Modal, TextInput, Select, Button, Flex, Text } from '@mantine/core';

// Profile type options
const PROFILE_TYPE_OPTIONS = [
  { value: 'ts', label: 'MPEG-TS Output (pipe:1)' },
  { value: 'hls', label: 'HLS Output (file-based)' },
];

const StreamProfile = ({ profile = null, isOpen, onClose }) => {
  const userAgents = useUserAgentsStore((state) => state.userAgents);

  const formik = useFormik({
    initialValues: {
      name: '',
      command: '',
      parameters: '',
      profile_type: 'ts',
      is_active: true,
      user_agent: '',
    },
    validationSchema: Yup.object({
      name: Yup.string().required('Name is required'),
      command: Yup.string().required('Command is required'),
      parameters: Yup.string().required('Parameters are is required'),
      profile_type: Yup.string().oneOf(['ts', 'hls']).required('Profile type is required'),
    }),
    onSubmit: async (values, { setSubmitting, resetForm }) => {
      if (profile?.id) {
        await API.updateStreamProfile({ id: profile.id, ...values });
      } else {
        await API.addStreamProfile(values);
      }

      resetForm();
      setSubmitting(false);
      onClose();
    },
  });

  useEffect(() => {
    if (profile) {
      formik.setValues({
        name: profile.name,
        command: profile.command,
        parameters: profile.parameters,
        profile_type: profile.profile_type || 'ts',
        is_active: profile.is_active,
        user_agent: profile.user_agent,
      });
    } else {
      formik.resetForm();
    }
  }, [profile]);

  if (!isOpen) {
    return <></>;
  }

  return (
    <Modal opened={isOpen} onClose={onClose} title="Stream Profile">
      <form onSubmit={formik.handleSubmit}>
        <TextInput
          id="name"
          name="name"
          label="Name"
          value={formik.values.name}
          onChange={formik.handleChange}
          error={formik.errors.name}
          disabled={profile ? profile.locked : false}
        />
        <TextInput
          id="command"
          name="command"
          label="Command"
          value={formik.values.command}
          onChange={formik.handleChange}
          error={formik.errors.command}
          disabled={profile ? profile.locked : false}
        />
        <TextInput
          id="parameters"
          name="parameters"
          label="Parameters"
          value={formik.values.parameters}
          onChange={formik.handleChange}
          error={formik.errors.parameters}
          disabled={profile ? profile.locked : false}
          description={
            formik.values.profile_type === 'hls'
              ? 'Use {streamUrl}, {userAgent}, and {hlsOutputPath} as placeholders'
              : 'Use {streamUrl} and {userAgent} as placeholders'
          }
        />

        <Select
          id="profile_type"
          name="profile_type"
          label="Output Type"
          value={formik.values.profile_type}
          onChange={(value) => formik.setFieldValue('profile_type', value)}
          error={formik.errors.profile_type}
          disabled={profile ? profile.locked : false}
          data={PROFILE_TYPE_OPTIONS}
          description="MPEG-TS outputs to pipe:1, HLS outputs to segment files"
        />

        <Select
          id="user_agent"
          name="user_agent"
          label="User-Agent"
          value={formik.values.user_agent}
          onChange={(value) => formik.setFieldValue('user_agent', value)}
          error={formik.errors.user_agent}
          data={userAgents.map((ua) => ({
            label: ua.name,
            value: `${ua.id}`,
          }))}
        />

        <Flex mih={50} gap="xs" justify="flex-end" align="flex-end">
          <Button
            type="submit"
            variant="contained"
            color="primary"
            disabled={formik.isSubmitting}
            size="small"
          >
            Submit
          </Button>
        </Flex>
      </form>
    </Modal>
  );
};

export default StreamProfile;
