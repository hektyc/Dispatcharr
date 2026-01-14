// Modal.js
import React, { useEffect, useMemo } from 'react';
import { useForm, Controller } from 'react-hook-form';
import { yupResolver } from '@hookform/resolvers/yup';
import * as Yup from 'yup';
import API from '../../api';
import useUserAgentsStore from '../../store/userAgents';
import { Modal, TextInput, Select, Button, Flex } from '@mantine/core';

// Profile type options
const PROFILE_TYPE_OPTIONS = [
  { value: 'ts', label: 'MPEG-TS Output (pipe:1)' },
  { value: 'hls', label: 'HLS Output (file-based)' },
];

const schema = Yup.object({
  name: Yup.string().required('Name is required'),
  command: Yup.string().required('Command is required'),
  parameters: Yup.string().required('Parameters are is required'),
  profile_type: Yup.string().oneOf(['ts', 'hls']).required('Profile type is required'),
});

const StreamProfile = ({ profile = null, isOpen, onClose }) => {
  const userAgents = useUserAgentsStore((state) => state.userAgents);

  const defaultValues = useMemo(
    () => ({
      name: profile?.name || '',
      command: profile?.command || '',
      parameters: profile?.parameters || '',
      profile_type: profile?.profile_type || 'ts',
      is_active: profile?.is_active ?? true,
      user_agent: profile?.user_agent || '',
    }),
    [profile]
  );

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
    reset,
    watch,
    control,
  } = useForm({
    defaultValues,
    resolver: yupResolver(schema),
  });

  const onSubmit = async (values) => {
    if (profile?.id) {
      await API.updateStreamProfile({ id: profile.id, ...values });
    } else {
      await API.addStreamProfile(values);
    }

    reset();
    onClose();
  };

  useEffect(() => {
    reset(defaultValues);
  }, [defaultValues, reset]);

  if (!isOpen) {
    return <></>;
  }

  const userAgentValue = watch('user_agent');
  const profileTypeValue = watch('profile_type');

  return (
    <Modal opened={isOpen} onClose={onClose} title="Stream Profile">
      <form onSubmit={handleSubmit(onSubmit)}>
        <TextInput
          label="Name"
          {...register('name')}
          error={errors.name?.message}
          disabled={profile ? profile.locked : false}
        />
        <TextInput
          label="Command"
          {...register('command')}
          error={errors.command?.message}
          disabled={profile ? profile.locked : false}
        />
        <TextInput
          label="Parameters"
          {...register('parameters')}
          error={errors.parameters?.message}
          disabled={profile ? profile.locked : false}
          description={
            profileTypeValue === 'hls'
              ? 'Use {streamUrl}, {userAgent}, and {hlsOutputPath} as placeholders'
              : 'Use {streamUrl} and {userAgent} as placeholders'
          }
        />

        <Controller
          name="profile_type"
          control={control}
          render={({ field }) => (
            <Select
              id="profile_type"
              label="Output Type"
              value={field.value}
              onChange={field.onChange}
              error={errors.profile_type?.message}
              disabled={profile ? profile.locked : false}
              data={PROFILE_TYPE_OPTIONS}
              description="MPEG-TS outputs to pipe:1, HLS outputs to segment files"
            />
          )}
        />

        <Select
          label="User-Agent"
          {...register('user_agent')}
          value={userAgentValue}
          error={errors.user_agent?.message}
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
            disabled={isSubmitting}
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
