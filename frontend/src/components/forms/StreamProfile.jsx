// Modal.js
import React, { useEffect, useState } from 'react';
import { useFormik } from 'formik';
import * as Yup from 'yup';
import API from '../../api';
import useUserAgentsStore from '../../store/userAgents';
import { Modal, TextInput, Textarea, Select, Button, Flex, Text, Alert } from '@mantine/core';
import { IconAlertCircle } from '@tabler/icons-react';

// Profile type options
const PROFILE_TYPE_OPTIONS = [
  { value: 'ts', label: 'MPEG-TS Output (pipe:1)' },
  { value: 'hls', label: 'HLS Output (file-based)' },
];

const StreamProfile = ({ profile = null, isOpen, onClose }) => {
  const userAgents = useUserAgentsStore((state) => state.userAgents);
  const [apiError, setApiError] = useState(null);

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
      parameters: Yup.string().required('Parameters are required'),
      profile_type: Yup.string().oneOf(['ts', 'hls']).required('Profile type is required'),
    }),
    onSubmit: async (values, { setSubmitting, resetForm, setErrors }) => {
      setApiError(null);
      try {
        if (profile?.id) {
          await API.updateStreamProfile({ id: profile.id, ...values });
        } else {
          await API.addStreamProfile(values);
        }

        resetForm();
        setSubmitting(false);
        onClose();
      } catch (error) {
        setSubmitting(false);
        // Handle validation errors from the API
        if (error.response?.data) {
          const errorData = error.response.data;
          // Check for field-specific errors
          if (errorData.parameters) {
            // Parameters validation error - display in a prominent alert
            const paramErrors = Array.isArray(errorData.parameters)
              ? errorData.parameters.join('\n\n')
              : errorData.parameters;
            setApiError(paramErrors);
          } else if (typeof errorData === 'object') {
            // Set field-level errors
            setErrors(errorData);
          } else if (typeof errorData === 'string') {
            setApiError(errorData);
          }
        } else {
          setApiError('An unexpected error occurred. Please try again.');
        }
      }
    },
  });

  useEffect(() => {
    setApiError(null);
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

  const handleClose = () => {
    setApiError(null);
    onClose();
  };

  if (!isOpen) {
    return <></>;
  }

  return (
    <Modal
      opened={isOpen}
      onClose={handleClose}
      title="Stream Profile"
      size={apiError ? "lg" : "md"}
    >
      <form onSubmit={formik.handleSubmit}>
        {apiError && (
          <Alert
            icon={<IconAlertCircle size={16} />}
            title="Validation Error"
            color="red"
            mb="md"
            style={{ whiteSpace: 'pre-wrap' }}
          >
            {apiError}
          </Alert>
        )}

        <TextInput
          id="name"
          name="name"
          label="Name"
          value={formik.values.name}
          onChange={formik.handleChange}
          error={formik.errors.name}
          disabled={profile ? profile.locked : false}
          mb="sm"
        />
        <TextInput
          id="command"
          name="command"
          label="Command"
          value={formik.values.command}
          onChange={formik.handleChange}
          error={formik.errors.command}
          disabled={profile ? profile.locked : false}
          mb="sm"
        />
        <Textarea
          id="parameters"
          name="parameters"
          label="Parameters"
          value={formik.values.parameters}
          onChange={formik.handleChange}
          error={formik.errors.parameters}
          disabled={profile ? profile.locked : false}
          description={
            formik.values.profile_type === 'hls'
              ? 'Use {streamUrl}, {userAgent}, {hlsOutputPath}, {segmentDuration}, {playlistSize}, {segmentExtension} as placeholders'
              : 'Use {streamUrl} and {userAgent} as placeholders'
          }
          minRows={3}
          autosize
          mb="sm"
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
          mb="sm"
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
          mb="sm"
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
